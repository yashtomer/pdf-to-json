# Architecture & Code Walkthrough

A plain-language guide to **how this service works** — what happens from the
moment a PDF is uploaded to the moment grouped JSON comes back. Read this
top-to-bottom to understand the whole flow.

---

## 1. The big picture in one diagram

```
   You (curl / browser / the team's app)
        │  POST a PDF  +  dpi
        ▼
┌─────────────────────────────────────────────────────────────┐
│  server.py            (FastAPI — the HTTP layer)             │
│   /extract-grouped  ── saves PDF to a temp file              │
│                       └─► calls extractor, then grouper      │
└───────────────┬─────────────────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────┐
│  extractor.py         (Surya 2 — the OCR/AI layer)           │
│   1. pdf2image  : render each PDF page → a picture (PNG)     │
│   2. Surya VLM  : "read" each picture → blocks of content    │
│                   (each block has a label + HTML)            │
│   returns: { pages: [ { page_number, blocks:[…] } ] }        │
└───────────────┬─────────────────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────┐
│  mpr_grouper.py       (the "make it tidy" layer)            │
│   reads the blocks' HTML, finds the <table>,                │
│   pulls work order + month + each employee row,             │
│   returns the clean grouped shape                           │
└───────────────┬─────────────────────────────────────────────┘
                ▼
   JSON back to you:
   [ { "work_order": "...", "mpr_month": "...",
       "employees": [ {employee_name, designation, leaves}, … ] } ]
```

**Three layers, three files.** Each does one job:

| File | Job | Analogy |
|---|---|---|
| `server.py` | Receive the HTTP request, hand off, send the answer back | The waiter |
| `extractor.py` | Turn a PDF into "what the page says" using the AI model | The reader |
| `mpr_grouper.py` | Turn "what the page says" into tidy structured data | The organiser |

---

## 2. What is Surya 2 (the AI model)?

Surya 2 is a **vision-language model** (a 650M-parameter neural network). You
give it a **picture of a page**, and it returns the page's content as a list of
**blocks** — and crucially, **tables come back as HTML `<table>` markup**.

It runs through an **inference backend** called `llama-server` (from the
llama.cpp project), which actually executes the neural network. On a GPU it's
fast (~1–2 s/page); on CPU it's slow (~minutes/page) but just as accurate.

The first time the server processes a PDF, it **downloads the model** (~1–2 GB)
from Hugging Face and caches it. After that, it's already on disk.

---

## 3. Step-by-step: a request through the system

### Step A — `server.py` receives the upload

When you call `POST /extract-grouped` with a PDF:

```python
# server.py
@app.post("/extract-grouped")
async def extract_grouped(file, dpi=300):
    # 1. save the uploaded bytes to a temp .pdf file on disk
    #    (pdf2image needs a file PATH, not raw bytes)
    tmp_path = <temp file>

    # 2. run the AI extraction
    raw = _extractor.extract_from_pdf(tmp_path, dpi=dpi)

    # 3. reshape into the tidy grouped form
    grouped = group_mpr(raw)

    # 4. delete the temp file, return the JSON
    return grouped
```

`_extractor` is created **once** when the server starts (see `lifespan` in
server.py) — that's when the model loads into memory. Loading it per-request
would be far too slow.

### Step B — `extractor.py` reads the PDF

```python
# extractor.py — SuryaExtractor.extract_from_pdf()
images = convert_from_path(pdf_path, dpi=dpi)   # PDF → list of page images
pages = []
for each image:
    result = self.recognition([image])          # the VLM "reads" the page
    blocks = result.blocks                       # list of content blocks
    pages.append({ "page_number": …, "blocks": blocks })
return { "file": …, "page_count": …, "pages": pages }
```

Each **block** looks like this (one block per logical region of the page):

```json
{
  "label": "Table",              // or "Text", "SectionHeader", "Picture", …
  "html": "<table>…</table>",    // the content, as HTML
  "bbox": [x0, y0, x1, y1],      // where it sits on the page
  "confidence": 0.98
}
```

So after this step we have, for an MPR page, blocks like:
- a `SectionHeader` block: `<h2>Monthly Performance Report</h2>`
- a `Text` block: `<p>Work Order No: M2602757</p>`
- a `Table` block: `<table>…the employee rows…</table>`

### Step C — `mpr_grouper.py` makes it tidy

This is where the messy HTML becomes the clean shape you want.

```python
# mpr_grouper.py — group_mpr()
for each page in the Surya result:
    page_text = all the blocks' text joined together

    work_order = find "M…" via regex            (_WORK_ORDER_RE)
    month      = find "MPR for the Month: April 2026"  (_MONTH_RE)

    table_html = the block whose label == "Table"
    rows       = _parse_table(table_html)        # HTML <table> → list of rows
    employees  = _employees_from_table(rows)     # rows → [{name, designation, leaves}]

    months.append({ work_order, mpr_month: month, employees })

_reconcile_roster(months)   # fill OCR gaps across months (see §5)
return months
```

---

## 4. The tricky part: reading the employee table

NICSI MPR tables are awkward, so `mpr_grouper.py` does two clever things.

### 4a. Parsing the HTML table → rows

`_parse_table()` uses Python's built-in `html.parser` to walk the
`<table>…</table>` and collect each `<tr>` (row) as a list of `<td>` cell
strings. One important detail: a `<br>` inside a cell (a line break) is turned
into a **space**, so a designation split across lines stays one clean string:

```
<td>Software Application<br/>Support Engineer</td>
   → "Software Application Support Engineer"
```

### 4b. Finding the right cells — "content-driven", not "column-driven"

You'd normally read a table by column position ("name is column 2"). **That
fails here** because NICSI tables use a *two-row header with a colspan*:

```
| SI | Name | Designation | Date of | Working Period | Absent |
|    |      |             | Joining |  From  |   To  |        |   ← "Working Period" spans 2 cols
```

The header has 6 columns but the data rows have 7 — so column numbers don't
line up. Instead, for each data row we find cells **by what they contain**:

```python
# mpr_grouper.py — _employees_from_table()
for each row:
    skip it if it's the footer ("Performance of the above…", "Signature…")
    designation = the cell that matches designation words
                  (Level N / Software Application / Increment / Tier …)   (_DESIG_HINTS)
    name        = a cell that looks like a person's name                 (_looks_like_name)
    leaves      = the LAST cell (the Absent column), parsed safely       (_leaves)
```

- **`_looks_like_name`** accepts Title-Case words, but rejects dates,
  designation text, and header words like "Date of Joining" (so headers don't
  get mistaken for names).
- **`_leaves`** returns `0` for `"-"`, blank, or a date that landed in that cell
  by OCR error — only a real 1–3 digit number becomes the leave count.
- **`_normalize_designation`** fixes common OCR quirks, e.g. `2"` → `2nd`, and
  inserts the dash in `…experience) - 3rd year 2nd Increment`.

---

## 5. Roster reconciliation — filling OCR gaps

A multi-page MPR is the **same work order** across several months, so the
employee list barely changes month to month. If OCR fails to read a name on one
page (a bad scan), we can recover it from another month.

`_reconcile_roster()`:
1. Builds a "best known" roster per work order — for each position, the first
   non-empty name seen, and the **longest** designation seen (OCR often
   truncates the designation on poorer pages).
2. Applies it back to every month: fills blank names, upgrades truncated
   designations, pads a month up to the full headcount.

It **never overwrites** a value that was read correctly — it only fills blanks
and lengthens truncated text. (Example: the April page of one test PDF was too
degraded for OCR to read the names, but reconciliation recovered "Ch. Kiran" /
"K Vijay" from the Feb/March pages — same work order, same roster.)

> This is an *inference*. If you want strictly-OCR'd data with no cross-month
> filling, that logic is the place to disable.

---

## 6. Where the speed goes (and why)

Surya 2 is a generative model, so it "writes out" the page content token by
token — that's inherently compute-heavy:

| Where it runs | Per page |
|---|---|
| x86_64 Docker under emulation (Intel Mac) | ~2.5–4 min |
| arm64-native Docker, CPU | ~16 s |
| native macOS + Metal GPU (`llama-server -ngl 99`) | ~9 s |
| NVIDIA GPU + vLLM backend | ~1–2 s |

The accuracy is the same everywhere — only speed changes. This is why a GPU host
matters for volume, and why the reverse proxy must allow long request times (a
page can take minutes on CPU).

---

## 7. File-by-file reference

| File | Key functions | Notes |
|---|---|---|
| `server.py` | `extract_grouped`, `extract`, `health`, `lifespan`, `run` | FastAPI. Model loads once in `lifespan`. `--workers 1`. |
| `extractor.py` | `SuryaExtractor.__init__`, `extract_from_pdf`, `extract_from_image`, `_block_to_dict` | Wraps Surya 2. Has an idempotent `shutil.move` patch so the model download survives retries. |
| `mpr_grouper.py` | `group_mpr`, `_parse_table`, `_employees_from_table`, `_looks_like_name`, `_leaves`, `_normalize_designation`, `_reconcile_roster`, `_parse_month_range`, `_parse_leave_certificate`, `_emp_leaves_for_month` | Pure Python + regex + stdlib `html.parser`. No extra deps. Also splits multi-month MPRs into per-month records using the Leave Adjustment Certificate pages. |
| `Dockerfile` | — | Multi-stage: bundles `llama-server`, installs Python deps. Venv at `/opt/venv`, `PYTHONPATH=/app`, runs `/opt/venv/bin/uvicorn` directly (so `/app` can be bind-mounted). |
| `docker-compose.yml` | — | `SURYA_HOST_PORT` (default 8000), named model-cache volume, restart policy, healthcheck, and a `./:/app:ro` bind mount so code edits need only `docker compose restart` (no rebuild). |

---

## 8. How to trace it yourself

Want to *see* each stage? Hit the two endpoints on the same PDF:

```bash
# raw — see exactly what Surya produced (blocks + html)
curl -F file=@samples/file.pdf -F dpi=150 \
     http://127.0.0.1:8000/extract -o raw.json

# grouped — see the tidy result the grouper made from those blocks
curl -F file=@samples/file.pdf -F dpi=150 \
     http://127.0.0.1:8000/extract-grouped -o grouped.json
```

Open `raw.json` and find a block with `"label": "Table"` — its `"html"` field is
the exact input `mpr_grouper.py` parses. Comparing `raw.json` (input) against
`grouped.json` (output) shows you precisely what the grouper does.
