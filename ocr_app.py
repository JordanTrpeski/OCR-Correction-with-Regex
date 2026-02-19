#!/usr/bin/env python3
"""
Mistral OCR Layer Tool
Applies OCR layer to images/PDFs using the Mistral AI OCR API.
Removes any existing text/OCR layer before re-applying.
"""

import os
import io
import base64
import threading
import queue
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import fitz          # PyMuPDF
from PIL import Image
from mistralai import Mistral

# ── Supported formats ──────────────────────────────────────────────────────────
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
SUPPORTED_DOCS   = {".pdf"}
SUPPORTED_ALL    = SUPPORTED_IMAGES | SUPPORTED_DOCS

OCR_MODEL = "mistral-ocr-latest"
RENDER_DPI = 300          # DPI when flattening PDF pages to images

# ── Palette ────────────────────────────────────────────────────────────────────
BG      = "#1e1e2e"
BG2     = "#2a2a3e"
BG3     = "#353550"
FG      = "#cdd6f4"
FG2     = "#a6adc8"
ACC     = "#89b4fa"     # blue
ACC2    = "#cba6f7"     # mauve
GREEN   = "#a6e3a1"
RED     = "#f38ba8"
YELLOW  = "#f9e2af"
BORDER  = "#45475a"

STATUS_CLR = {
    "Pending":    YELLOW,
    "Processing": ACC,
    "Done":       GREEN,
    "Error":      RED,
}


# ══════════════════════════════════════════════════════════════════════════════
#  OCR Engine
# ══════════════════════════════════════════════════════════════════════════════

def has_text_layer(pdf_path: str) -> bool:
    """Return True if any page in the PDF contains selectable text."""
    doc = fitz.open(pdf_path)
    for page in doc:
        if page.get_text("text").strip():
            doc.close()
            return True
    doc.close()
    return False


def flatten_pdf_to_images(pdf_path: str, dpi: int = RENDER_DPI) -> list[bytes]:
    """Render every PDF page to a PNG byte-string (strips any text layer)."""
    doc = fitz.open(pdf_path)
    pages_bytes = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for page in doc:
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        pages_bytes.append(pix.tobytes("png"))
    doc.close()
    return pages_bytes


def image_to_base64(path: str) -> tuple[str, str]:
    """Return (mime_type, base64_data) for a local image file."""
    ext = Path(path).suffix.lower().lstrip(".")
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",  "tiff": "image/tiff",
        "tif": "image/tiff", "bmp":  "image/bmp",
        "webp": "image/webp",
    }
    mime = mime_map.get(ext, "image/png")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return mime, data


def bytes_to_base64(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def build_searchable_pdf(
    image_sources: list,        # list of (bytes | str path)
    pages_markdown: list[str],  # OCR markdown per page
    output_path: str,
) -> None:
    """
    Create a searchable PDF: original images as visible background,
    OCR text as invisible overlay (render_mode=3).
    """
    out_doc = fitz.open()

    for idx, (img_src, markdown) in enumerate(zip(image_sources, pages_markdown)):
        # --- load image into PIL to get dimensions ---
        if isinstance(img_src, bytes):
            pil_img = Image.open(io.BytesIO(img_src))
        else:
            pil_img = Image.open(img_src)

        w_px, h_px = pil_img.size
        # Convert pixels → points (72 pt/inch; render DPI used for PDFs)
        # For images we don't know DPI reliably, use PIL's info or default 96
        dpi_val = pil_img.info.get("dpi", (96, 96))
        if isinstance(dpi_val, tuple):
            dpi_val = dpi_val[0]
        dpi_val = dpi_val if dpi_val else 96
        w_pt = w_px * 72 / dpi_val
        h_pt = h_px * 72 / dpi_val

        page = out_doc.new_page(width=w_pt, height=h_pt)

        # --- insert the image as background ---
        if isinstance(img_src, bytes):
            img_rect = fitz.Rect(0, 0, w_pt, h_pt)
            page.insert_image(img_rect, stream=img_src)
        else:
            img_rect = fitz.Rect(0, 0, w_pt, h_pt)
            page.insert_image(img_rect, filename=img_src)

        # --- overlay invisible OCR text ---
        _insert_invisible_text(page, markdown, w_pt, h_pt)

    out_doc.save(output_path, garbage=4, deflate=True)
    out_doc.close()


def _insert_invisible_text(page, markdown: str, w_pt: float, h_pt: float):
    """Distribute OCR text lines invisibly across the page."""
    lines = [l for l in markdown.splitlines() if l.strip()]
    if not lines:
        return

    font_size = 10
    line_h = font_size * 1.4
    margin_x = 10
    margin_y = 20
    usable_h = h_pt - margin_y * 2

    # spread lines evenly; if they fit naturally use natural spacing
    natural_h = len(lines) * line_h
    step = line_h if natural_h <= usable_h else usable_h / len(lines)

    for i, line in enumerate(lines):
        y = margin_y + i * step + font_size
        if y > h_pt - margin_y:
            break
        try:
            page.insert_text(
                (margin_x, y),
                line,
                fontsize=font_size,
                render_mode=3,   # 3 = invisible (PDF spec)
                color=(0, 0, 0),
            )
        except Exception:
            pass


def process_file(
    path: str,
    api_key: str,
    output_dir: str,
    save_markdown: bool,
    log_fn,
    progress_fn,
) -> str:
    """
    Full pipeline for one file.
    Returns output path on success or raises an exception.
    """
    src = Path(path)
    client = Mistral(api_key=api_key)
    out_stem = src.stem + "_ocr"

    log_fn(f"[{src.name}] Starting …")

    # ── 1. Prepare image(s) ──────────────────────────────────────────────────
    is_pdf = src.suffix.lower() == ".pdf"
    image_sources = []     # list[bytes | str]  – one entry per page
    mistral_docs  = []     # documents to send to Mistral, one per page

    if is_pdf:
        text_found = has_text_layer(str(src))
        if text_found:
            log_fn(f"[{src.name}] Existing OCR layer detected – removing …")
        else:
            log_fn(f"[{src.name}] No existing text layer found.")

        log_fn(f"[{src.name}] Rasterising PDF pages at {RENDER_DPI} DPI …")
        pages_png = flatten_pdf_to_images(str(src), dpi=RENDER_DPI)
        for png_bytes in pages_png:
            image_sources.append(png_bytes)
            data_uri = bytes_to_base64(png_bytes, "image/png")
            mistral_docs.append({"type": "image_url", "image_url": data_uri})
    else:
        mime, b64 = image_to_base64(str(src))
        image_sources.append(str(src))
        mistral_docs.append({
            "type": "image_url",
            "image_url": f"data:{mime};base64,{b64}",
        })

    total_pages = len(image_sources)
    log_fn(f"[{src.name}] {total_pages} page(s) to process.")

    # ── 2. OCR each page ─────────────────────────────────────────────────────
    pages_markdown = []
    for i, doc_item in enumerate(mistral_docs):
        log_fn(f"[{src.name}] OCR page {i+1}/{total_pages} …")
        progress_fn(i / total_pages)

        resp = client.ocr.process(
            model=OCR_MODEL,
            document=doc_item,
            include_image_base64=False,
        )

        # Extract markdown from response
        md_text = ""
        if hasattr(resp, "pages") and resp.pages:
            md_text = resp.pages[0].markdown or ""
        elif hasattr(resp, "text"):
            md_text = resp.text or ""

        pages_markdown.append(md_text)

    progress_fn(0.9)

    # ── 3. Save outputs ──────────────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)

    # Searchable PDF
    pdf_out = str(Path(output_dir) / (out_stem + ".pdf"))
    log_fn(f"[{src.name}] Building searchable PDF …")
    build_searchable_pdf(image_sources, pages_markdown, pdf_out)

    # Markdown / plain text
    if save_markdown:
        md_out = str(Path(output_dir) / (out_stem + ".md"))
        combined_md = f"\n\n---\n\n".join(pages_markdown)
        Path(md_out).write_text(combined_md, encoding="utf-8")
        log_fn(f"[{src.name}] Markdown saved → {md_out}")

    progress_fn(1.0)
    log_fn(f"[{src.name}] Done → {pdf_out}")
    return pdf_out


# ══════════════════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════════════════

INPUT_FOLDER = Path(__file__).parent / "input"

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mistral OCR Layer Tool")
        self.geometry("920x680")
        self.minsize(760, 560)
        self.configure(bg=BG)

        self._stop_event = threading.Event()
        self._log_queue  = queue.Queue()
        self._file_rows  = {}   # path → iid
        self._worker     = None

        self._build_ui()
        self._poll_log()
        self.after(100, self._load_input_folder)

    # ── UI construction ────────────────────────────────────────────────────
    def _build_ui(self):
        self._style()

        # ── Top bar: API key ──────────────────────────────────────────────
        top = tk.Frame(self, bg=BG, pady=8, padx=12)
        top.pack(fill="x")

        tk.Label(top, text="API Key:", bg=BG, fg=FG2, font=("Segoe UI", 9)).pack(side="left")
        self.api_var = tk.StringVar(value=os.environ.get("MISTRAL_API_KEY", ""))
        api_entry = tk.Entry(top, textvariable=self.api_var, bg=BG3, fg=FG,
                             insertbackground=FG, relief="flat", font=("Segoe UI", 9),
                             width=38, show="•")
        api_entry.pack(side="left", padx=(6, 4))
        self._show_key = False
        tk.Button(top, text="Show", bg=BG3, fg=FG2, relief="flat",
                  activebackground=BG3, activeforeground=FG,
                  command=lambda: self._toggle_key(api_entry),
                  cursor="hand2").pack(side="left", padx=(0, 16))

        tk.Button(top, text="Test Key", bg=ACC, fg=BG, relief="flat",
                  activebackground=ACC2, font=("Segoe UI", 9, "bold"),
                  command=self._test_key, cursor="hand2").pack(side="left")

        # ── File list ─────────────────────────────────────────────────────
        list_frame = tk.Frame(self, bg=BG, padx=12)
        list_frame.pack(fill="both", expand=True)

        btn_row = tk.Frame(list_frame, bg=BG)
        btn_row.pack(fill="x", pady=(0, 4))
        for lbl, cmd in [("+ Add Files", self._add_files),
                          ("+ Add Folder", self._add_folder),
                          ("Clear All", self._clear_all)]:
            tk.Button(btn_row, text=lbl, bg=BG3, fg=FG, relief="flat",
                      activebackground=BORDER, activeforeground=FG,
                      command=cmd, cursor="hand2",
                      font=("Segoe UI", 9), padx=10, pady=4).pack(side="left", padx=(0, 4))

        # Treeview
        cols = ("file", "type", "status")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                 selectmode="extended", style="OCR.Treeview")
        self.tree.heading("file",   text="File")
        self.tree.heading("type",   text="Type")
        self.tree.heading("status", text="Status")
        self.tree.column("file",   width=480, stretch=True)
        self.tree.column("type",   width=80,  anchor="center")
        self.tree.column("status", width=100, anchor="center")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<BackSpace>", lambda e: self._remove_selected())

        # ── Options row ───────────────────────────────────────────────────
        opts = tk.Frame(self, bg=BG, padx=12, pady=6)
        opts.pack(fill="x")

        # Output dir
        tk.Label(opts, text="Output folder:", bg=BG, fg=FG2,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar(value=str(Path.home() / "Desktop" / "OCR Output"))
        tk.Entry(opts, textvariable=self.out_var, bg=BG3, fg=FG,
                 insertbackground=FG, relief="flat",
                 font=("Segoe UI", 9), width=38).grid(row=0, column=1, padx=(6, 4), sticky="we")
        tk.Button(opts, text="Browse…", bg=BG3, fg=FG2, relief="flat",
                  activebackground=BORDER, command=self._browse_out,
                  cursor="hand2").grid(row=0, column=2, padx=(0, 20))

        # Save markdown checkbox
        self.md_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Also save Markdown (.md)", variable=self.md_var,
                       bg=BG, fg=FG, selectcolor=BG3,
                       activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 9)).grid(row=0, column=3, sticky="w")

        opts.columnconfigure(1, weight=1)

        # ── Progress + actions ────────────────────────────────────────────
        prog_frame = tk.Frame(self, bg=BG, padx=12, pady=4)
        prog_frame.pack(fill="x")

        self.progress = ttk.Progressbar(prog_frame, style="OCR.Horizontal.TProgressbar",
                                        mode="determinate", length=300)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.run_btn = tk.Button(prog_frame, text="▶  Process All",
                                 bg=GREEN, fg=BG, relief="flat",
                                 font=("Segoe UI", 10, "bold"),
                                 activebackground=GREEN, activeforeground=BG,
                                 command=self._start, cursor="hand2",
                                 padx=16, pady=6)
        self.run_btn.pack(side="left", padx=(0, 6))

        self.stop_btn = tk.Button(prog_frame, text="■  Stop",
                                  bg=BG3, fg=RED, relief="flat",
                                  font=("Segoe UI", 10),
                                  activebackground=BORDER,
                                  command=self._stop, cursor="hand2",
                                  padx=12, pady=6, state="disabled")
        self.stop_btn.pack(side="left")

        self.status_lbl = tk.Label(prog_frame, text="Ready", bg=BG, fg=FG2,
                                   font=("Segoe UI", 9))
        self.status_lbl.pack(side="right")

        # ── Log ───────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=BG, padx=12, pady=(0, 8))
        log_frame.pack(fill="x")

        tk.Label(log_frame, text="Log", bg=BG, fg=FG2,
                 font=("Segoe UI", 8)).pack(anchor="w")

        self.log_text = tk.Text(log_frame, height=7, bg=BG2, fg=FG2,
                                insertbackground=FG, relief="flat",
                                font=("Cascadia Code", 8),
                                state="disabled", wrap="word")
        log_sb = ttk.Scrollbar(log_frame, orient="vertical",
                               command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self.log_text.pack(fill="x")

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("OCR.Treeview",
                     background=BG2, foreground=FG, fieldbackground=BG2,
                     rowheight=26, borderwidth=0,
                     font=("Segoe UI", 9))
        s.configure("OCR.Treeview.Heading",
                     background=BG3, foreground=FG2,
                     relief="flat", font=("Segoe UI", 9, "bold"))
        s.map("OCR.Treeview",
              background=[("selected", BG3)],
              foreground=[("selected", ACC)])
        s.configure("OCR.Horizontal.TProgressbar",
                     troughcolor=BG3, background=ACC, borderwidth=0)

    # ── Input folder auto-load ────────────────────────────────────────────
    def _load_input_folder(self):
        if not INPUT_FOLDER.exists():
            return
        found = 0
        for f in sorted(INPUT_FOLDER.iterdir()):
            if f.is_file() and f.suffix.lower() in SUPPORTED_ALL:
                self._enqueue_file(str(f))
                found += 1
        if found:
            self._log(f"[Startup] Loaded {found} file(s) from input\\")

    # ── File management ───────────────────────────────────────────────────
    def _add_files(self):
        types = [
            ("Supported files", " ".join(f"*{e}" for e in sorted(SUPPORTED_ALL))),
            ("Images", " ".join(f"*{e}" for e in sorted(SUPPORTED_IMAGES))),
            ("PDF files", "*.pdf"),
            ("All files", "*.*"),
        ]
        paths = filedialog.askopenfilenames(filetypes=types)
        for p in paths:
            self._enqueue_file(p)

    def _add_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        for root, _, files in os.walk(folder):
            for f in files:
                if Path(f).suffix.lower() in SUPPORTED_ALL:
                    self._enqueue_file(os.path.join(root, f))

    def _enqueue_file(self, path: str):
        if path in self._file_rows:
            return
        ext = Path(path).suffix.lower()
        ftype = "PDF" if ext == ".pdf" else "Image"
        iid = self.tree.insert("", "end",
                               values=(path, ftype, "Pending"),
                               tags=("Pending",))
        self.tree.tag_configure("Pending",    foreground=YELLOW)
        self.tree.tag_configure("Processing", foreground=ACC)
        self.tree.tag_configure("Done",       foreground=GREEN)
        self.tree.tag_configure("Error",      foreground=RED)
        self._file_rows[path] = iid

    def _remove_selected(self):
        for iid in self.tree.selection():
            vals = self.tree.item(iid, "values")
            if vals:
                self._file_rows.pop(vals[0], None)
            self.tree.delete(iid)

    def _clear_all(self):
        self.tree.delete(*self.tree.get_children())
        self._file_rows.clear()

    def _browse_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    def _toggle_key(self, entry):
        self._show_key = not self._show_key
        entry.configure(show="" if self._show_key else "•")

    # ── API key test ──────────────────────────────────────────────────────
    def _test_key(self):
        key = self.api_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Please enter an API key.")
            return

        def _check():
            try:
                client = Mistral(api_key=key)
                # Lightweight call – list models
                client.models.list()
                self._log("[API] Key is valid ✓")
                self.status_lbl.config(text="API key OK", fg=GREEN)
            except Exception as e:
                self._log(f"[API] Key test failed: {e}")
                self.status_lbl.config(text="API key failed", fg=RED)

        threading.Thread(target=_check, daemon=True).start()

    # ── Processing ────────────────────────────────────────────────────────
    def _start(self):
        files = [self.tree.item(iid, "values")[0]
                 for iid in self.tree.get_children()
                 if self.tree.item(iid, "values")[2] != "Done"]
        if not files:
            messagebox.showinfo("Nothing to do",
                                "Add files first (or all files are already done).")
            return

        key = self.api_var.get().strip()
        if not key:
            messagebox.showwarning("No Key", "Enter a Mistral API key.")
            return

        self._stop_event.clear()
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress["value"] = 0
        self.status_lbl.config(text="Processing…", fg=ACC)

        self._worker = threading.Thread(
            target=self._worker_fn,
            args=(files, key, self.out_var.get(), self.md_var.get()),
            daemon=True,
        )
        self._worker.start()

    def _stop(self):
        self._stop_event.set()
        self._log("[Control] Stop requested …")
        self.stop_btn.config(state="disabled")

    def _worker_fn(self, files, key, out_dir, save_md):
        total = len(files)
        done = 0
        errors = 0

        for path in files:
            if self._stop_event.is_set():
                self._log("[Control] Processing stopped by user.")
                break

            iid = self._file_rows.get(path)
            self._set_status(iid, "Processing")

            def _prog(frac, _done=done, _total=total):
                overall = (_done + frac) / _total * 100
                self.after(0, lambda v=overall: self.progress.configure(value=v))

            try:
                process_file(
                    path=path,
                    api_key=key,
                    output_dir=out_dir,
                    save_markdown=save_md,
                    log_fn=self._log,
                    progress_fn=_prog,
                )
                self._set_status(iid, "Done")
                done += 1
            except Exception as exc:
                self._log(f"[ERROR] {Path(path).name}: {exc}")
                self._set_status(iid, "Error")
                errors += 1

        # Final update
        summary = f"Finished: {done} done, {errors} error(s)."
        self._log(f"[Summary] {summary}")
        fg = GREEN if errors == 0 else YELLOW
        stopped = self._stop_event.is_set()
        self.after(0, lambda s=summary, c=fg, st=stopped: self._on_done(s, c, st))

    def _on_done(self, summary: str, fg: str, stopped: bool):
        if not stopped:
            self.progress.configure(value=100)
        self.status_lbl.config(text=summary, fg=fg)
        self.run_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def _set_status(self, iid, status: str):
        if iid is None:
            return
        self.after(0, lambda i=iid, s=status: self._apply_status(i, s))

    def _apply_status(self, iid, status: str):
        self.tree.item(iid, tags=(status,))
        self.tree.set(iid, "status", status)

    # ── Log ───────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        self._log_queue.put(msg)

    def _poll_log(self):
        while not self._log_queue.empty():
            msg = self._log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(100, self._poll_log)


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.mainloop()
