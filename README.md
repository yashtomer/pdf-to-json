# pdf-reader

A robust Python PDF reader that extracts text **and tables** from **any** PDF —
whether it contains a real text layer or is just a scanned image. It
auto-detects which case you're in and picks the right strategy:

1. **Native-text PDFs** → extracted with [`pdfplumber`](https://github.com/jsvine/pdfplumber) (layout-aware, fast). Tables come back **cell-aware**, so multi-line wrapped cells (like "Software Application Support Engineer (4 to less than 6 years…)") are returned as a single string, not split across rows.
2. **Scanned / image-only PDFs** → automatically fall back to OCR using
   [`pdf2image`](https://github.com/Belval/pdf2image) (renders the page as an image)
   + [`pytesseract`](https://github.com/madmaze/pytesseract) (reads text out of the image with Tesseract).
3. **Common JSON output** — every PDF can be dumped to a per-file `<stem>.json` with the same schema (file, metadata, pages, text, tables, error), so downstream code never has to handle special cases.
4. Per-page error handling — one bad page can't kill the whole job.

## Tested against

| File type | Producer | Pages | Method picked |
|---|---|---:|---|
| Digitally-signed Work Order | PDFium | 3 | text |
| Digitally-signed Work Order | Oracle XML Publisher (AcroForm) | 2 | text |
| Multi-page scan | VersaLink C7130 Color MFP | 3 | OCR |
| Image-only print | Microsoft Print To PDF | 1 | OCR |

All 11 pages across 4 files read with zero errors.

## Prerequisites

You need two system binaries and one Python toolchain:

| Tool | Why | Install (macOS) |
|---|---|---|
| `tesseract` | OCR engine used by `pytesseract` | `brew install tesseract` |
| `poppler` | PDF rendering used by `pdf2image` | `brew install poppler` |
| [`uv`](https://docs.astral.sh/uv/) | Python project + dependency manager (recommended) | `brew install uv` |

> On Linux: `apt install tesseract-ocr poppler-utils` (Debian/Ubuntu) or
> `dnf install tesseract poppler-utils` (Fedora). `uv` install instructions are
> on [the uv site](https://docs.astral.sh/uv/getting-started/installation/).

## Setup

You have **two ways** to install the Python dependencies. Pick one.

### Option A — using uv (recommended)

```bash
git clone <this-repo>     # or however you got the code
cd pdf-reader
uv sync                   # creates .venv, installs locked versions from uv.lock
```

That's it. No `source activate` needed — uv handles the virtual environment automatically.

### Option B — using plain pip (no uv)

```bash
cd pdf-reader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# Single file
uv run pdf-reader path/to/file.pdf

# Run against every PDF in the samples/ folder
uv run pdf-reader samples/*.pdf

# Multiple files; also write each extracted text to out/<filename>.txt
uv run pdf-reader samples/*.pdf --dump-text out/

# Dump one structured JSON file per PDF (common schema for every file)
uv run pdf-reader samples/*.pdf --dump-json out/

# Print everything as a single JSON array to stdout (handy for piping)
uv run pdf-reader file.pdf --json

# Password-protected PDF
uv run pdf-reader file.pdf --password "secret"

# Disable OCR — scanned pages will come back empty (faster)
uv run pdf-reader file.pdf --no-ocr

# Change OCR resolution (default 400 — higher = slower but more accurate)
uv run pdf-reader file.pdf --dpi 500

# Split combined tax cells ("18.00% 56,844.80") into separate rate & amount fields
uv run pdf-reader samples/*.pdf --split-tax-cells --dump-json out/

# Include the raw per-page 'text' field in JSON output (excluded by default)
uv run pdf-reader samples/*.pdf --include-text --dump-json out/
```

If you're not using uv, just replace `uv run pdf-reader` with `python pdf_reader.py` (after activating your venv).

### Sample output

```
=== samples/m2602757signed.pdf ===
  Pages: 3
  Producer: PDFium
  [Page 1] OK via text, 5375 chars, 2 tables / 13 rows
  [Page 2] OK via text, 8653 chars
  [Page 3] OK via text, 4212 chars

=== samples/mprs_feb_mar_apr_2026.pdf ===
  Pages: 3
  Producer: VersaLink C7130 Color MFP
  [Page 1] OK via ocr, 598 chars
  [Page 2] OK via ocr, 585 chars
  [Page 3] OK via ocr, 580 chars
```

Each page reports the method used (`text`, `ocr`, or `empty`), the character
count, and (where present) the number of tables and rows found.

### Common JSON schema (one file per PDF)

When you pass `--dump-json out/`, each input PDF produces `out/<stem>.json`
with this shape:

```json
{
  "file": "samples/m2602757signed.pdf",
  "page_count": 3,
  "metadata": { "Producer": "PDFium", "CreationDate": "D:20260519150923" },
  "pages": [
    {
      "page_number": 1,
      "method": "text",
      "char_count": 5375,
      "fields": {
        "work_order_no": "M2602757",
        "date": "19-MAY-2026",
        "project_no": "S220466MPTS",
        "project_name": "SVP National Police Academy",
        "name": "Aeologic Technologies Pvt. Ltd.",
        "address": "Block C-324,25,26,27 3rd Floor, ...",
        "contact_person": "Vikrant Kumar",
        "phone_no": "9873759782",
        "email_id": "hr.support@aeologic.com",
        "subject": "Work-Order for providing Office Support and Project Management Support and Rollout Services as mentioned above."
      },
      "tables": [
        [
          {
            "s_no": "1",
            "hsn_sac_code": "998313",
            "description": "Software Application Support Engineer (4 to less than 6 years relevant experience)-3rd year 2nd Increment",
            "no_of_persons_required": "2",
            "required_period_no_of_months_days": "Three Month(s) and Nineteen Day(s)",
            "unit_rate_per_month_excluding_taxes": "44,042.14",
            "date_of_deployment_from_to": "09/02/2026 To 27/05/2026",
            "total_amount_axbxc": "3,15,804.47",
            "igst_pct_amount": "18.00% 56,844.80"
          }
        ]
      ],
      "error": null
    }
  ],
  "error": null
}
```

The same schema is produced for every PDF — text-based or scanned. Scanned
pages have `"method": "ocr"` and `"tables": []` since OCR output has no
inherent cell structure.

> The raw per-page `text` field is **excluded** from JSON output by default
> (it's bulky and structured data is what you usually want). If you need
> it, pass `--include-text` to put it back in. The full text is also
> available as separate `.txt` files via `--dump-text DIR`.

### `fields` — structured key-value pairs

Most PDFs have a header box at the top with label/value pairs (Work Order No,
Date, Project Name, Issued to / Name / Address / Phone / Email …). Even
though pdfplumber returns these as a "table", they are really a key-value box,
not tabular data.

The reader auto-detects these (a table is considered key-value when ≥20% of
its non-empty cells end with `:` or `:-`, or match well-known label words
like `Date`), flattens them into the `fields` dict on the page, and **omits
them from `tables`** — so `tables` contains only real data tables.

Additional fields are pulled from free-form text via regex on every page
(so continuation pages and OCR pages also contribute). The current set of
patterns extracts:

| Field | Type | Pattern looked for |
|---|---|---|
| `subject` | string | `Subject: …` |
| `work_order_no` | string | `Work Order No:- …` |
| `project_no` | string | `Project No.: …` |
| `po_no` | string | `PO No.: …` |
| `empanelment_no` | string | `Empanelment No: …` |
| `valid_till` | string | `Valid Till: …` |
| `gstin` | string | `GSTIN: …` |
| `issuing_authority` | string | `For <authority>` (signature block) |
| `signed_by` | string | `Digitally signed by <name>` (when digitally signed) |
| `signed_on` | string | `Date: <timestamp>` after signature line |
| `signatory_name` | string | `(<name>)` in signature block |
| `signatory_designation` | string | 1-3 lines after `(<name>)` before `Copy To:` |
| `copy_to` | **list of strings** | Numbered list under `Copy To:` |

Most fields are strings, but `copy_to` is a **list of strings** — Python and
JSON both handle mixed-type dicts natively. The `fields` type signature is
`dict[str, str | list[str]]`.

Easy to extend — most patterns are one line in `_TEXT_FIELD_PATTERNS` inside
[`pdf_reader.py`](pdf_reader.py). The signature block and `Copy To` list
have dedicated parsers (`_extract_signature_block`, `_extract_copy_to`).
Table-extracted fields always win over text-extracted ones for the same key
(text patterns use `setdefault`).

### What gets filtered out of `tables`

Two kinds of "junk" rows are dropped automatically when building keyed
tables:

1. **Column-letter annotation rows** — e.g. `(A) (B) (C) (D) …` directly
   under the column headers. These reference formula columns (like
   `(AxBxC)` in the header) and are not actual data.
2. **Key-value boxes** — see above, routed to `fields` instead.

Totals rows (e.g. `Total Amount in Rs. …`, `Grand Total (in Rs.):-`) are
kept inside the table — they're aggregate data, still useful — but the
first cell's value (e.g. `"Total Amount in Rs."`) will appear under the
first column's key. You can identify them client-side by checking whether
the first column starts with `Total` / `Grand Total`.

### Table shape and key naming

Each page has a single `tables` field — a list of tables, where each table is
a list of row-dicts. The first row of every table is used as the **column
headers** for all subsequent rows. Keys are normalized to `snake_case`:

| PDF header text | JSON key |
|---|---|
| `Description` | `description` |
| `S. No` | `s_no` |
| `HSN/ SAC Code` | `hsn_sac_code` |
| `No of Persons Required` | `no_of_persons_required` |
| `Required Period (No. of Months/ days)` | `required_period_no_of_months_days` |
| `Unit Rate per Month (excluding Taxes)` | `unit_rate_per_month_excluding_taxes` |
| `Date of Deployment (From/To)` | `date_of_deployment_from_to` |
| `Total Amount (AxBxC)` | `total_amount_axbxc` |
| `CGST (%)` | `cgst_pct` |
| `IGST Amount` | `igst_amount` |

**Slugification rules:** lowercase, `%` → `pct`, any run of non-alphanumeric
characters → single `_`, leading/trailing `_` stripped. Empty headers become
`col_1`, `col_2`, …; duplicates get `_2`, `_3` suffixes.

### Splitting combined tax cells (`--split-tax-cells`)

Some PDFs stack two values in a single visual cell — e.g. NICSI Work Orders
put the GST **rate** on top and **amount** below, both inside one column
header `CGST (%) /Amount`. By default this comes out as a single field:

```json
"CGST (%) /Amount": "0.00% 0.00",
"IGST (%) /Amount": "18.00% 56,844.80"
```

Add `--split-tax-cells` and the reader rewrites every `<tax>_pct_amount`
field into two separate fields:

```json
"cgst_pct": "0.00%",
"cgst_amount": "0.00",
"igst_pct": "18.00%",
"igst_amount": "56,844.80"
```

In totals rows (where only the amount is present, no percentage), the rate
field is correctly left empty — no values are fabricated:

```json
"cgst_pct": "",
"cgst_amount": "0.00",
"igst_pct": "",
"igst_amount": "56,844.80"
```

Programmatic equivalent:

```python
doc = read_pdf("invoice.pdf", split_tax_cells=True)
```

### Header / value cleanup

Cells that wrap across multiple visual lines in the PDF are joined into one
clean string. Three small heuristics are applied automatically:

| Wrapped in PDF | Becomes |
|---|---|
| `Person`\n`s` | `Persons` (single trailing lowercase letter joins) |
| `Require`\n`d` | `Required` |
| `14,09,808.9`\n`8` | `14,09,808.98` (single trailing digit joins to a number) |
| `2,53,765`\n`.62` | `2,53,765.62` (decimal/comma continuation joins) |
| `Three Month(s)`\n`and Nineteen`\n`Day(s)` | `Three Month(s) and Nineteen Day(s)` (multi-word line breaks become spaces) |

## How it works

For every page in the PDF:

1. Try `pdfplumber.Page.extract_text()` — fast, preserves layout.
2. Try `pdfplumber.Page.extract_tables()` — returns each table as a list of
   rows, each row a list of cell strings. Multi-line wrapped cells are joined
   internally (newlines collapsed to spaces) so each cell is one clean string.
3. If the result of step 1 has fewer than **30 characters**, the page is
   treated as scanned. The page is re-rendered as a **400-DPI** image
   (override with `--dpi`), then OCR'd by Tesseract.
4. Any per-page exception is captured and recorded (not raised) so the rest of
   the document still processes.

The threshold of 30 chars is tuned to catch nearly-empty pages (just a page
number, footer, etc.) and route them to OCR. Adjust `MIN_CHARS_PER_PAGE` in
[`pdf_reader.py`](pdf_reader.py) if your PDFs need a different cutoff.

## Project layout

```
pdf-reader/
├── pdf_reader.py        # the reader (CLI + library)
├── pyproject.toml       # project metadata + declared dependencies
├── uv.lock              # exact pinned dependency graph (managed by uv)
├── requirements.txt     # direct dependencies only — for pip users
├── README.md            # this file
├── .gitignore
├── samples/             # local PDFs to test against (gitignored — see note below)
└── .venv/               # virtual environment (created by uv sync — do not edit)
```

> ⚠️ **`samples/` is gitignored.** PDFs in this folder are treated as local
> test data and excluded from version control by the `samples/*.pdf` /
> `samples/*.PDF` rules in `.gitignore`. This protects any sensitive content
> (employee names, salaries, signatures, internal documents) from being
> accidentally committed and pushed to a public repository. The empty folder
> itself is preserved via `samples/.gitkeep`.

## Why these three libraries?

The Python PDF ecosystem is large; this project deliberately picks the
**simplest reliable stack with no commercial-licensing landmines**:

| Library | Job | License |
|---|---|---|
| [`pdfplumber`](https://github.com/jsvine/pdfplumber) | Read text and tables from native-text PDFs | MIT |
| [`pdf2image`](https://github.com/Belval/pdf2image) | Render a PDF page as an image | MIT |
| [`pytesseract`](https://github.com/madmaze/pytesseract) | Run Tesseract OCR on an image | Apache 2 |

**Notable alternatives intentionally avoided:** PyMuPDF (`fitz`) is faster
and more feature-complete but is **AGPL-licensed** — not safe for
closed-source or commercial use without a paid commercial license. The
three libraries above are all MIT/Apache and safe everywhere.

## Programmatic use — `pdf_to_json()`

The one-liner you'll want to use from another Python project:

```python
from pdf_reader import pdf_to_json

data = pdf_to_json("invoice.pdf")
# data is a regular Python dict — pass to json.dumps(), send over HTTP,
# write to a database, manipulate however you like.

print(data["pages"][0]["fields"]["work_order_no"])
for row in data["pages"][0]["tables"][0]:
    print(row["description"], row["total_amount_axbxc"])
```

### Signature

```python
pdf_to_json(
    path: str | Path,
    *,
    split_tax_cells: bool = True,   # split "X (%) /Amount" cells into <x>_pct / <x>_amount
    include_text: bool = False,     # include the bulky raw 'text' field
    ocr_dpi: int = 400,             # render scanned pages at this DPI for OCR
    password: str = "",             # for encrypted PDFs
    as_string: bool = False,        # True → returns JSON string; False → returns dict
) -> dict | str
```

The dict matches the schema documented above — `file`, `page_count`,
`metadata`, `pages[*]` (each with `fields`, `tables`, etc.), `error`.

### Using this package from another project

The cleanest way is to install this folder as an editable dependency in
the other project. From inside your other project's directory:

```bash
# With uv (recommended)
uv add /usr/local/var/www/pdf-reader

# Or with plain pip
pip install -e /usr/local/var/www/pdf-reader
```

Then just import it:

```python
from pdf_reader import pdf_to_json
data = pdf_to_json("some-file.pdf")
```

When you make changes to `pdf_reader.py` here, the consuming project picks
them up automatically (that's what editable installs mean).

### Lower-level API

If you need per-page iteration with dataclass typing (rather than a plain
dict), use `read_pdf()` directly:

```python
from pdf_reader import read_pdf, DocumentResult

doc: DocumentResult = read_pdf("invoice.pdf", split_tax_cells=True)
print(doc.page_count, doc.full_text)
for page in doc.pages:
    print(page.page_number, page.method, page.char_count, page.fields)
```

## Dependency management cheat-sheet

```bash
uv sync                  # install everything from uv.lock
uv tree                  # see the full dependency tree
uv add some-library      # add a new dependency
uv remove some-library   # remove one
uv lock --upgrade        # bump within version specs

# Regenerate requirements.txt from uv.lock (full pinned, including transitive)
uv export --format requirements-txt --no-hashes --no-emit-project --output-file requirements.txt
```
