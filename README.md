# OCR Correction with Regex

Two tools for producing clean, searchable PDFs from scanned documents using [Mistral OCR](https://mistral.ai), with a rule-based post-correction layer that fixes common OCR mistakes in engineering document IDs.

---

## Requirements

- **Python 3.10 or newer** — [python.org/downloads](https://www.python.org/downloads/)
  ⚠️ During install, tick **"Add Python to PATH"**
- **A Mistral API key** — free at [console.mistral.ai](https://console.mistral.ai)

No Node.js or anything else needed.

---

## Setup (Windows — one time only)

```
1. Download or clone this repo
2. Double-click setup.bat
3. Paste your Mistral API key when prompted
```

That's it. `setup.bat` installs all dependencies and saves your key locally.

---

## How to use

### GUI — `ocr_app.py` (batch processing, point-and-click)

```
python ocr_app.py
```

Add PDFs or images, pick an output folder, click **Process All**.

### CLI — `ria.py` (single or folder, with correction layer)

```
1. Drop your PDFs into the  input\  folder
2. Run:  python ria.py
3. Pick up corrected PDFs from the  output\  folder
```

Or point at a single file:
```bash
python ria.py path\to\file.pdf
python ria.py path\to\file.pdf -o corrected.pdf --md
```

---

## What the correction does

After OCR, a two-pass correction engine cleans up common mistakes in engineering document IDs (format: `26437-RIA-001-DR-CLG-PC-00001`):

**Pass 1 — ordered regex rules:**

| OCR output | Corrected | Reason |
|---|---|---|
| `OOI` | `001` | O=0, I=1 |
| `PRII` / `PRI` | `PR1` | I misread as 1 |
| `INII` / `INI` | `IN1` | |
| `GRII` / `GRI` | `GR1` | |
| `1N2` | `IN2` | 1 misread as I |
| `P[I/1]E` | `PLE` | I/1 misread as L |
| `028` | `02B` | 8 misread as B |
| `040` | `04C` | 0 misread as C |
| `0P` | `OP` | O over-zeroed |
| `0TH` | `OTH` | O over-zeroed |
| `0002B` | `00028` | B/8 confusion |

**Pass 2 — document-ID structural correction:**
Finds IDs in the text and converts O→0 / I→1 in segments that are always all-digit (project number and sequence number).

The terminal log shows every rule that fired, every ID corrected, and a summary of total fixes.
