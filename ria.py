#!/usr/bin/env python3
"""
RIA OCR Corrector
Rasterise → Mistral OCR → rule-based correction → searchable PDF

Usage:
    python ria.py INPUT.pdf
    python ria.py INPUT.pdf -o corrected.pdf --md
    python ria.py INPUT.pdf --dpi 200 --no-ocr-strip
"""

import sys
import os
import io
import re
import base64
import argparse
import difflib
from pathlib import Path

import fitz          # PyMuPDF
from PIL import Image
from mistralai import Mistral

# ── Enable ANSI on Windows ────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("")   # activates VT-100 sequences in Windows console

# ── ANSI colour helpers ───────────────────────────────────────────────────────
R  = "\033[0m"        # reset
B  = "\033[1m"        # bold
DIM= "\033[2m"
CY = "\033[36m"       # cyan
GR = "\033[32m"       # green
YE = "\033[33m"       # yellow
RE = "\033[31m"       # red
MA = "\033[35m"       # magenta
BL = "\033[34m"       # blue

def hdr(s):  return f"{B}{CY}{s}{R}"
def ok(s):   return f"{GR}{s}{R}"
def warn(s): return f"{YE}{s}{R}"
def err(s):  return f"{RE}{s}{R}"
def hi(s):   return f"{MA}{s}{R}"
def dim(s):  return f"{DIM}{s}{R}"


# ── Config ────────────────────────────────────────────────────────────────────
API_KEY   = os.environ.get("MISTRAL_API_KEY", "")
OCR_MODEL = "mistral-ocr-latest"
RENDER_DPI = 300


# ══════════════════════════════════════════════════════════════════════════════
#  Correction rules  (applied in this exact order — most specific first)
# ══════════════════════════════════════════════════════════════════════════════
#
#  Source: the Replace-text rules observed in the Make/Integromat automation
#  that was correcting document IDs before upload.
#
#  Bracket notation used in that tool maps to regex character classes:
#    [O]   = literal O
#    [II]  = character class {I} (both chars identical, so just I)
#    [1II] = character class {1, I}
#    [I1]  = character class {I, 1}
#    [8]   = literal 8
#
#  Order: multi-char literals → prefix-anchored patterns → single-char swaps
#         → post-global-O-fix corrections (0P → OP, 0TH → OTH, …)
# ─────────────────────────────────────────────────────────────────────────────

RULES: list[tuple[re.Pattern, str, str]] = [
    # ── (regex_pattern,  replacement,  human-readable label) ─────────────────

    # 1. Longest/most-specific multi-char literals first
    (re.compile(r"0002B"),          "00028",  "0002B → 00028  [B↔8]"),
    (re.compile(r"OOI"),            "001",    "OOI → 001      [O=0, I=1]"),

    # 2. Prefix-anchored I/1 confusions  (OCR reads 1 as I)
    (re.compile(r"PR[I1l]{1,2}"),   "PR1",    "PR{I|II|1} → PR1"),
    (re.compile(r"IN[I1l]{1,2}"),   "IN1",    "IN{I|II|1} → IN1"),
    (re.compile(r"GR[I1l]{1,2}"),   "GR1",    "GR{I|II|1} → GR1"),

    # 3. Leading-char ambiguous sequences
    (re.compile(r"[1Il]N2"),        "IN2",    "[1/I]N2 → IN2"),
    (re.compile(r"[1Il]N1"),        "IN1",    "[1/I]N1 → IN1"),

    # 4. Single-char swaps inside known surrounds
    (re.compile(r"P[I1]E"),         "PLE",    "P[I/1]E → PLE  [I=L, 1=L]"),
    (re.compile(r"028"),            "02B",    "028 → 02B       [8=B]"),
    (re.compile(r"040"),            "04C",    "040 → 04C       [0=C]"),

    # 5. Post "global O→0" over-corrections (digit 0 where letter O is correct)
    (re.compile(r"0P(?=[^0-9])"),   "OP",     "0P → OP         [O over-zeroed]"),
    (re.compile(r"0TH"),            "OTH",    "0TH → OTH       [O over-zeroed]"),
]


# ── Document-ID regex ─────────────────────────────────────────────────────────
#
#  Target format (from project spec):
#      \d{5}-[A-Z]{3}-\d{3}-[A-Z]{2}-[A-Z]{2,3}-[A-Z]{2,3}-\d{5}
#  Example:   26437-RIA-001-DR-CLG-PC-00001
#
#  Lenient capture pattern (tolerates OCR noise):
#      (\d{2,6})-(\w{2,4})-(\w{2,5})-(\w{2,3})-(\w{2,5})-(\w{2,5})-(\w{4,6})
#
#  Anchored on the observed 5-digit project prefix.  Segment groups:
#      1 → project  (should be ALL digits)
#      2 → org code (should be ALL letters, e.g. RIA)
#      3 → series   (mostly digits; may end with revision letter e.g. 04C)
#      4 → doc type (ALL letters, e.g. DR, SH)
#      5 → discipline (ALL letters, e.g. CLG, GEM)
#      6 → sub-code  (ALL letters, e.g. PC, ID)
#      7 → sequence  (should be ALL digits, e.g. 00001)
# ─────────────────────────────────────────────────────────────────────────────

DOC_ID_RE = re.compile(
    r"(?<!\w)"
    r"(\d{2,6})"          # seg 1 – project number
    r"-([\w]{2,4})"       # seg 2 – org code
    r"-([\w]{2,5})"       # seg 3 – series / revision
    r"-([\w]{2,3})"       # seg 4 – document type
    r"-([\w]{2,5})"       # seg 5 – discipline
    r"-([\w]{2,5})"       # seg 6 – sub-code
    r"-([\w]{4,6})"       # seg 7 – sequence number
    r"(?!\w)"
)

# Segment correction mode:
#   'D' = expect all digits → convert O→0, I→1
#   'M' = mixed / unknown  → leave alone (literal rules handle these)
#
# Only seg 1 (project number e.g. 26437) and seg 7 (sequence e.g. 00205) are
# guaranteed all-digit.  All other segments may contain legitimate digit chars
# (e.g. GR1, 02A, 04C) so we do NOT blindly convert them.
SEG_TYPE = ["D", "M", "M", "M", "M", "M", "D"]


def _fix_digit_seg(s: str) -> str:
    """Convert OCR-confused chars to digits (for all-digit segments)."""
    return (s.replace("O", "0").replace("o", "0")
             .replace("I", "1").replace("l", "1").replace("L", "1"))


def _fix_doc_id(m: re.Match) -> tuple[str, list[tuple[str, str]]]:
    """Return (corrected_id, [(seg_raw, seg_fixed), ...]) for a regex match."""
    segs_raw = list(m.groups())
    segs_fix = []
    changes  = []

    for raw, kind in zip(segs_raw, SEG_TYPE):
        fixed = _fix_digit_seg(raw) if kind == "D" else raw
        segs_fix.append(fixed)
        if fixed != raw:
            changes.append((raw, fixed))

    return "-".join(segs_fix), changes


# ══════════════════════════════════════════════════════════════════════════════
#  Correction engine
# ══════════════════════════════════════════════════════════════════════════════

class PageCorrector:
    """Applies the full correction pipeline to one page of OCR text and logs changes."""

    def __init__(self, page_num: int):
        self.page        = page_num
        self.rule_hits   : list[tuple[str, str, str, int]] = []   # (find, replace, label, count)
        self.id_changes  : list[tuple[str, str, list]]     = []   # (raw_id, fixed_id, seg_changes)
        self.total_fixes  = 0

    # ── Pass 1: ordered literal/regex rules ──────────────────────────────────
    def _apply_rules(self, text: str) -> str:
        for pat, repl, label in RULES:
            new_text, n = pat.subn(repl, text)
            if n:
                self.rule_hits.append((pat.pattern, repl, label, n))
                self.total_fixes += n
                text = new_text
        return text

    # ── Pass 2: document-ID structural correction ─────────────────────────────
    def _apply_id_fixes(self, text: str) -> str:
        def _sub(m):
            raw_id = m.group(0)
            fixed_id, seg_changes = _fix_doc_id(m)
            if fixed_id != raw_id:
                self.id_changes.append((raw_id, fixed_id, seg_changes))
                self.total_fixes += 1
            return fixed_id
        return DOC_ID_RE.sub(_sub, text)

    # ── Public ────────────────────────────────────────────────────────────────
    def correct(self, text: str) -> str:
        text = self._apply_rules(text)
        text = self._apply_id_fixes(text)
        return text

    def print_report(self):
        indent = "      "
        if not self.rule_hits and not self.id_changes:
            print(f"    {ok('✓')} no corrections needed")
            return

        if self.rule_hits:
            print(f"    {hdr('Rule corrections:')}")
            for _, _, label, n in self.rule_hits:
                print(f"    {indent}{ok('→')} {label}  {dim(f'×{n}')}")

        if self.id_changes:
            print(f"    {hdr('Document-ID corrections:')}")
            for raw, fixed, seg_ch in self.id_changes:
                print(f"    {indent}{warn(raw)}")
                print(f"    {indent}{ok(fixed)}")
                for s_raw, s_fix in seg_ch:
                    print(f"    {indent}  {dim(s_raw)} → {hi(s_fix)}")


# ══════════════════════════════════════════════════════════════════════════════
#  OCR engine  (reused pattern from ocr_app.py)
# ══════════════════════════════════════════════════════════════════════════════

def flatten_pdf(pdf_path: str, dpi: int = RENDER_DPI) -> list[bytes]:
    """Render every page to PNG bytes at given DPI (strips any existing text layer)."""
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pages = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        pages.append(pix.tobytes("png"))
    doc.close()
    return pages


def ocr_page(client: Mistral, png_bytes: bytes) -> str:
    """Run Mistral OCR on a PNG, return raw markdown."""
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
    resp = client.ocr.process(
        model=OCR_MODEL,
        document={"type": "image_url", "image_url": data_uri},
        include_image_base64=False,
    )
    if hasattr(resp, "pages") and resp.pages:
        return resp.pages[0].markdown or ""
    return ""


# ── PDF assembly ──────────────────────────────────────────────────────────────

def _insert_invisible_text(page, text: str, w_pt: float, h_pt: float):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return
    font_size = 10
    line_h    = font_size * 1.4
    margin_x  = 10
    margin_y  = 20
    usable_h  = h_pt - margin_y * 2
    step = line_h if len(lines) * line_h <= usable_h else usable_h / len(lines)
    for i, line in enumerate(lines):
        y = margin_y + i * step + font_size
        if y > h_pt - margin_y:
            break
        try:
            page.insert_text(
                (margin_x, y), line,
                fontsize=font_size,
                render_mode=3,      # invisible (PDF spec)
                color=(0, 0, 0),
            )
        except Exception:
            pass


def build_pdf(png_pages: list[bytes], texts: list[str], out_path: str):
    out = fitz.open()
    for png_bytes, text in zip(png_pages, texts):
        pil = Image.open(io.BytesIO(png_bytes))
        w_px, h_px = pil.size
        dpi_info = pil.info.get("dpi", (96, 96))
        dpi_val  = (dpi_info[0] if isinstance(dpi_info, tuple) else dpi_info) or 96
        w_pt, h_pt = w_px * 72 / dpi_val, h_px * 72 / dpi_val

        page = out.new_page(width=w_pt, height=h_pt)
        page.insert_image(fitz.Rect(0, 0, w_pt, h_pt), stream=png_bytes)
        _insert_invisible_text(page, text, w_pt, h_pt)

    out.save(out_path, garbage=4, deflate=True)
    out.close()


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

HERE_DIR   = Path(__file__).parent
INPUT_DIR  = HERE_DIR / "input"
OUTPUT_DIR = HERE_DIR / "output"


def parse_args():
    p = argparse.ArgumentParser(
        description=(
            "RIA OCR Corrector – Mistral OCR + rule-based correction → searchable PDF\n"
            "\n"
            "No arguments:  process every PDF in the input\\ folder → output\\ folder\n"
            "With a file:   process that single PDF"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input",  nargs="?", default=None,
                   help="Input PDF file (omit to use input\\ folder)")
    p.add_argument("-o", "--output", default=None,
                   help="Output PDF path (single-file mode only)")
    p.add_argument("--md",  action="store_true",
                   help="Also write corrected text as .md file")
    p.add_argument("--api-key", default=API_KEY,
                   help="Mistral API key (overrides built-in default)")
    return p.parse_args()


def _sep(char="─", n=70):
    print(dim(char * n))


# ── Single-file processor ─────────────────────────────────────────────────────

def process_file(src: Path, out_pdf: Path, out_md: Path | None, client: Mistral):
    """Full pipeline for one PDF. Returns total corrections made."""

    _sep("═")
    print(f"  {B}{src.name}{R}")
    _sep("═")
    print(f"  {dim('Input')}   {src}")
    print(f"  {dim('Output')}  {out_pdf}")
    if out_md:
        print(f"  {dim('Markdown')} {out_md}")
    print(f"  {dim('DPI')}     {RENDER_DPI}")
    _sep()

    # ── Step 1: Rasterise ─────────────────────────────────────────────────────
    print(f"\n{hdr('[ 1 / 3 ]  Rasterising PDF')}")
    print(f"  Rendering at {RENDER_DPI} DPI (strips existing text layer)…")
    pages_png = flatten_pdf(str(src), dpi=RENDER_DPI)
    n = len(pages_png)
    print(f"  {ok(f'{n} page(s) ready.')}")

    # ── Step 2: OCR + correction ───────────────────────────────────────────────
    print(f"\n{hdr('[ 2 / 3 ]  OCR  +  Correction')}")
    _sep()

    corrected_texts  = []
    total_page_fixes = 0

    for i, png in enumerate(pages_png):
        pg = i + 1
        print(f"\n  {B}Page {pg}/{n}{R}")

        # OCR
        print(f"    {dim('→ Mistral OCR…')}  ", end="", flush=True)
        raw   = ocr_page(client, png)
        chars = len(raw)
        lines = raw.count("\n") + 1
        print(f"{ok('done')}  {dim(f'{chars:,} chars, {lines} lines')}")

        # Correction
        print(f"    {dim('→ Correcting…')}")
        corrector = PageCorrector(pg)
        fixed     = corrector.correct(raw)
        corrector.print_report()

        if fixed != raw:
            delta = len(fixed) - chars
            sign  = "+" if delta >= 0 else ""
            print(f"    {dim(f'Length: {chars:,} → {len(fixed):,}  ({sign}{delta})')}")
        if corrector.total_fixes:
            total_page_fixes += corrector.total_fixes

        corrected_texts.append(fixed)
        _sep()

    # ── Step 3: Build output ───────────────────────────────────────────────────
    print(f"\n{hdr('[ 3 / 3 ]  Building output')}")

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Assembling searchable PDF…", end="", flush=True)
    build_pdf(pages_png, corrected_texts, str(out_pdf))
    size_kb = out_pdf.stat().st_size // 1024
    print(f" {ok('saved')}  {dim(f'{size_kb:,} KB')}  →  {out_pdf.name}")

    if out_md:
        out_md.write_text("\n\n---\n\n".join(corrected_texts), encoding="utf-8")
        print(f"  Markdown written  →  {out_md.name}")

    _sep("═")
    if total_page_fixes:
        print(f"  {ok('✓ Done.')}  {warn(f'{total_page_fixes} correction(s)')} applied across {n} page(s).")
    else:
        print(f"  {ok('✓ Done.')}  No corrections needed — OCR output looked clean.")
    _sep("═")
    print()

    return total_page_fixes


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    key  = args.api_key or os.environ.get("MISTRAL_API_KEY", "")
    if not key:
        print(err("Error: no Mistral API key found."))
        print(dim("  Set the MISTRAL_API_KEY environment variable, or pass --api-key KEY"))
        sys.exit(1)
    client = Mistral(api_key=key)

    # ══ Folder mode (no argument given) ══════════════════════════════════════
    if args.input is None:
        INPUT_DIR.mkdir(exist_ok=True)
        OUTPUT_DIR.mkdir(exist_ok=True)

        pdfs = sorted(INPUT_DIR.glob("*.pdf"))

        _sep("═")
        print(f"  {B}RIA OCR Corrector  —  Folder mode{R}")
        _sep("═")
        print(f"  {dim('Input folder')}   {INPUT_DIR}")
        print(f"  {dim('Output folder')}  {OUTPUT_DIR}")
        print()

        if not pdfs:
            print(warn("  No PDF files found in the input\\ folder."))
            print(dim("  Drop your PDFs into:"))
            print(f"  {INPUT_DIR}")
            print()
            return

        print(f"  Found {ok(str(len(pdfs)))} PDF(s) to process:\n")
        for p in pdfs:
            print(f"    {dim('·')} {p.name}")
        print()

        grand_total = 0
        for idx, src in enumerate(pdfs, 1):
            print(f"\n{hdr(f'  FILE {idx}/{len(pdfs)}')}\n")
            out_pdf = OUTPUT_DIR / src.name
            out_md  = (OUTPUT_DIR / src.stem).with_suffix(".md") if args.md else None
            try:
                fixes = process_file(src, out_pdf, out_md, client)
                grand_total += fixes
            except Exception as exc:
                print(err(f"  ERROR processing {src.name}: {exc}"))

        _sep("═")
        print(f"  {B}All done.{R}  {warn(str(grand_total))} total correction(s) across {len(pdfs)} file(s).")
        _sep("═")
        print()
        return

    # ══ Single-file mode ══════════════════════════════════════════════════════
    src = Path(args.input)
    if not src.exists():
        print(err(f"Error: file not found: {src}"))
        sys.exit(1)
    if src.suffix.lower() != ".pdf":
        print(err("Error: only PDF input is supported."))
        sys.exit(1)

    out_pdf = Path(args.output) if args.output else OUTPUT_DIR / src.name
    out_md  = out_pdf.with_suffix(".md") if args.md else None

    process_file(src, out_pdf, out_md, client)


if __name__ == "__main__":
    main()
