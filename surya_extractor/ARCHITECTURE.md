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
    month      = find the MPR month             (_find_month)

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
    designation = the cell that matches designation words                (_DESIG_HINTS)
                  (Level N / Software Application / Increment / Tier …)
                  …or, if none matches but the row has a date, the cell
                  right after the name (a plain job title like
                  "GIS DIGITIZATION SUPERVISOR")
    name        = a cell that looks like a person's name                 (_looks_like_name)
    keep the row only if it resolved to a designation
    leaves      = the LAST cell (the Absent column), parsed safely       (_leaves)
```

- **`_looks_like_name`** accepts Title-Case *and* ALL-CAPS names (e.g.
  "RAKESH KUMAR SINGH"), but rejects dates, designation text, and header phrases
  whose every word is a header word ("Date of Joining", "AGENCY Name", "New
  Designation") so headers don't get mistaken for names.
- **Requiring a designation** is what drops header rows and other non-employee
  rows — they resolve to no designation. The fallback (the cell after the name,
  guarded by the row having a date) lets MPRs that use plain job titles instead
  of "Level N" still extract, without re-admitting header rows (which have no
  date).
- **`_leaves`** reads the *leading* integer as the count — the Absent column is
  often "`2 (02.01.2026 & 19.01.2026)`", where the parenthetical just lists which
  days. It returns `0` for `"-"`, blank, or a bare date that landed in the cell
  by OCR error.
- **`_normalize_designation`** fixes common OCR quirks, e.g. `2"` → `2nd`, and
  inserts the dash in `…experience) - 3rd year 2nd Increment`.

### 4c. Choosing the right table, and stitching split tables

Two more things happen around `_employees_from_table`:

- **Justification grids are excluded.** Some MPRs append a "Justification for
  Attendance not marked" table (columns #, Date, Day, Reason). Its *Day* cells
  ("Friday") would look like names, so `_is_justification_table()` filters those
  out before a page's employee table is picked — a table with a "Designation"
  column is always treated as the employee table.
- **Continuation pages are merged** (`_merge_continuation_pages`). Employee
  tables routinely span a page break (employees 1-5 on one page, 6+ on the next
  with no month label of its own), and some MPRs put each employee on a separate
  page. A month-less page inherits the previous same-work-order month, then all
  records sharing `(work_order, month)` merge into one — employees concatenated,
  de-duplicated by name. This is why a 12-page, 4-work-order MPR comes back as 4
  clean records, not 12 fragments.

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
| `mpr_grouper.py` | `group_mpr`, `_parse_table`, `_employees_from_table`, `_is_justification_table`, `_merge_continuation_pages`, `_looks_like_name`, `_leaves`, `_find_month`, `_normalize_designation`, `_reconcile_roster`, `_parse_month_range`, `_parse_leave_certificate`, `_emp_leaves_for_month` | Pure Python + regex + stdlib `html.parser`. No extra deps. `_find_month` handles label variants, numeric `MM/YYYY`, and bare months (year from the page). Excludes justification grids, merges page-split / one-per-page tables, and splits multi-month MPRs into per-month records using the Leave Adjustment Certificate pages. |
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
