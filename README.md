# OCR Correction with Regex

Two tools for producing clean, searchable PDFs from scanned documents using [Mistral OCR](https://mistral.ai), with a rule-based post-correction layer that fixes common OCR mistakes in engineering document IDs.

---

## Tools

### `ocr_app.py` — GUI batch processor

Point-and-click interface for processing multiple files at once.

```
python ocr_app.py
```

- Add PDFs or images via the GUI
- Strips any existing text layer, re-OCRs with Mistral
- Outputs a searchable PDF with invisible text overlay

### `ria.py` — CLI corrector

Command-line tool with a two-pass correction engine on top of Mistral OCR.

```bash
# Drop PDFs into input/ and run:
python ria.py

# Or point at a single file:
python ria.py path/to/file.pdf
python ria.py path/to/file.pdf -o corrected.pdf --md
```

**Pipeline per page:**
1. Rasterise the page at 300 DPI (strips existing text layer)
2. OCR with `mistral-ocr-latest`
3. **Pass 1 — Ordered regex rules** (fixes known character confusions)
4. **Pass 2 — Document-ID structural correction** (segment-aware O↔0 / I↔1 fixes)
5. Assemble searchable PDF (image background + invisible text overlay)

---

## Correction rules

The rule pass targets common OCR mistakes seen in engineering document IDs of the form:

```
26437-RIA-001-DR-CLG-PC-00001
```

| OCR output | Corrected | Reason |
|---|---|---|
| `OOI` | `001` | O=0, I=1 |
| `PRII` / `PRI` | `PR1` | I misread as 1 |
| `INII` / `INI` | `IN1` | |
| `GRII` / `GRI` | `GR1` | |
| `1N2` | `IN2` | 1 misread as I |
| `P[I/1]E` | `PLE` | I or 1 misread as L |
| `028` | `02B` | 8 misread as B |
| `040` | `04C` | 0 misread as C |
| `0P` | `OP` | O over-zeroed |
| `0TH` | `OTH` | O over-zeroed |
| `0002B` | `00028` | B/8 confusion |

Document-ID segments that are always all-digits (project number, sequence number) additionally get O→0 and I→1 applied structurally.

---

## Requirements

```
pip install mistralai pymupdf pillow
```

Python 3.10+

---

## API key

Set your Mistral API key as an environment variable:

```bash
# Windows
set MISTRAL_API_KEY=your_key_here

# macOS / Linux
export MISTRAL_API_KEY=your_key_here
```

Get a key at [console.mistral.ai](https://console.mistral.ai).
