# Architecture & Code Walkthrough — langchain-pdf

A plain-language guide to **how this service works** — from the moment a document
is uploaded to the moment structured JSON comes back. This is the **live** service
(`https://pdfparser.aeologic.in`). It started as an MPR extractor on **Anthropic
Claude** and now does **two document types** (MPR + NICSI Work Order) across
**three engines** (Claude, Google Gemini, local Ollama) — **6 endpoints** in all.
It accepts **PDFs *or* images** (phone photos of MPRs are common). (For the
on-prem Surya fallback, see `../surya_extractor/ARCHITECTURE.md`.)

The MPR-on-Claude flow below (§1–§6) is the core; §6b–§6d cover work orders, the
deterministic reliability layers, and the other engines.

---

## 1. The big picture in one diagram

```
   You (curl / the team's app)
        │  POST a PDF
        ▼
┌─────────────────────────────────────────────────────────────┐
│  app/main.py          (FastAPI — the HTTP layer)            │
│   POST /extract-grouped ── saves PDF to a temp file          │
│                          └─► runs the extractor in a thread  │
└───────────────┬─────────────────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────┐
│  app/extractor.py     (the "read + structure" layer)        │
│   1. pdf2image  : render each page → an image (downscaled)   │
│   2. LangChain  : ONE Claude call with all page images + the │
│                   rules prompt + the schema (tool-calling)   │
│   3. merge      : consolidate rows sharing (work_order,month)│
└───────────────┬─────────────────────────────────────────────┘
                ▼
   JSON back to you:
   [ { "work_order": "...", "mpr_month": "...",
       "employees": [ {employee_name, designation, leaves}, … ] } ]
```

**The files and their jobs:**

| File | Job |
|---|---|
| `app/main.py` | HTTP layer: the 6 endpoints, auth, upload validation, threadpool offload |
| `app/extractor.py` | MPR via Claude; `load_page_images` (PDF *or* image); `SYSTEM_PROMPT`; merge |
| `app/workorder.py` | Work Order via Claude; `reconcile_workorder` reliability layers; scanned-doc ensemble |
| `app/gemini.py` | MPR + Work Order via Google Gemini |
| `app/mpr_local.py` / `app/workorder_local.py` | the same two jobs via **local Ollama** |
| `app/schemas.py` | Pydantic models — define the output shape *and* instruct the model |
| `app/config.py` | `.env` → typed settings |

For the cloud engines there is **no OCR engine and no GPU** — the model does both
the reading *and* the structuring in one call. The cleverness that needed ~600
lines of parser code in the Surya version lives in a **prompt** (see §4) plus a
small deterministic reconciliation layer for work orders (§6c).

---

## 2. Why Claude instead of an OCR model?

MPR PDFs are **dynamic** (every agency's layout differs) and often **scanned**.
A rules-based parser breaks on each new layout. A vision-language model reads the
table *semantically* and adapts — so new formats mostly "just work" without code
changes. Trade-off: a few cents per document, and the pages leave your box (to
Anthropic). At ~550 MPRs/month that's ~$8 on Sonnet (~$4 with the batch path).

---

## 3. Step-by-step: a request through the system

### Step A — `app/main.py` receives the upload

```python
@app.post("/extract-grouped", dependencies=[Depends(require_api_key)])
async def extract_grouped_endpoint(file):
    # 0. require_api_key: check X-API-Key against API_AUTH_KEYS (401 if bad)
    # 1. _validate_upload: accept pdf / jpg / png / webp / tif / … (not just PDF)
    # 2. save the uploaded bytes to a temp file
    # 3. run the blocking work in a thread so the event loop stays free:
    return await asyncio.to_thread(extract_grouped, tmp_path)
    # 4. delete the temp file
```

The six extraction endpoints all follow this shape (auth → validate → temp file →
threadpool). They differ only in the extractor they call — `/extract-grouped`
(Claude), `/extract-grouped-gemini`, `/extract-grouped-qwen3-vl` for MPRs, and the
three `/extract-workorder*` for work orders.

**Auth.** `/extract-grouped` is gated by `require_api_key` — callers send
`X-API-Key: <key>`, checked (constant-time) against `API_AUTH_KEYS` from `.env`.
Empty list = open. `/health` and `/docs` stay public so the Docker healthcheck and
Swagger work. The scheme is registered with FastAPI's `APIKeyHeader`, so Swagger
shows an **Authorize** button.

Running the blocking call (`asyncio.to_thread`) means `/health` stays responsive
and **many uploads can be in flight at once** — each is waiting on Anthropic's
network call, which parallelises well.

### Step B — `app/extractor.py` reads + structures

```python
# extractor.py — extract_grouped()
image_blocks = _pdf_to_image_blocks(pdf_path)        # PDF → list of page images
content = [ {"type":"text","text":"Extract the MPR…"}, *image_blocks ]
structured = ChatAnthropic(...).with_structured_output(MPRDocument)
result = structured.invoke([ _system_message(), HumanMessage(content) ])
return _merge_by_work_order_month(result.records)
```

Three things happen:

1. **`_pdf_to_image_blocks`** calls **`load_page_images`**, which detects the
   upload by content (`%PDF-` magic bytes): a PDF is rendered with `pdf2image`, an
   **image** (jpg/png/…) is opened directly with PIL — so a phone photo of an MPR
   works the same as a PDF. Each page is **downscaled** to `IMAGE_MAX_EDGE` (don't
   pay for resolution the model won't use) and encoded as a base64 **JPEG** block.
2. **One Claude call** via LangChain `with_structured_output(MPRDocument)`. This
   forces Claude to answer by calling a tool whose arguments are our schema — so
   the reply is already validated structured data, no parsing. All pages go in
   **one** message, so Claude resolves continuation pages, multi-month splits and
   multi-work-order tables holistically.
3. **`_merge_by_work_order_month`** (see §6) tidies the grouping.

### Step C — the result

A `list[MPRRecord]` (Pydantic), which FastAPI serialises to the JSON shape above.

---

## 4. Where the intelligence lives: the system prompt

This is the heart of the service. `SYSTEM_PROMPT` in `extractor.py` encodes every
MPR rule we learned (the hard way, on the Surya version):

- **Per-row work orders** — if a table has a `Work Order No.`/`Wos` column, split
  into one record per work order.
- **Leaves** — use the `Total Absence`/`Leaves Taken` count, *not* the Remarks
  text, a date, or an attendance time; `"2 (02.01.2026 & 19.01.2026)"` → 2.
- **Multi-month** — a range like "January to March 2026" + Leave Adjustment
  Certificates → one record per month with that month's leaves.
- **Grouped names** — `"1. A 2. B 3. C"` in one cell → one employee each.
- **Continuation pages** — same work order spilling onto the next page → one record.
- **Ignore** "Justification for Attendance" detail grids.
- Keep ALL-CAPS names as printed; never invent a name or number.

To change behaviour, you edit this prompt — not parser code.

---

## 5. The schema does double duty (`app/schemas.py`)

The Pydantic models define the output **and** instruct Claude. The `Field(...)`
descriptions are sent to the model as the tool definition, so they're extraction
instructions too:

```python
class Employee(BaseModel):
    employee_name: str = Field(description="full name as printed; ALL-CAPS ok; "
                                            "empty if unreadable — never guess")
    designation:   str = Field(description="full role text …")
    leaves:      float = Field(description="Total Absence count; 0.5/1.5 allowed; "
                                            "not a remark/date/time; '-' or blank = 0")

class MPRRecord(BaseModel):   work_order, mpr_month, employees: list[Employee]
class MPRDocument(BaseModel): records: list[MPRRecord]   # the tool's root
```

Because `with_structured_output` validates against these models, a malformed
answer makes the model retry — you always get well-typed data.

---

## 6. The one bit of post-processing — `_merge_by_work_order_month`

Claude sometimes emits **one record per row** when a work order spans several
rows (e.g. file13 came back as 9 records instead of 7). This deterministic step
groups records that share the same `(work_order, mpr_month)` into one,
concatenating their employees (order preserved). Distinct work orders or months
stay separate. It's the only "parsing" code left, and it's ~10 lines.

---

## 6b. Work Orders (`app/workorder.py`)

A NICSI Work Order is a different document → `POST /extract-workorder` →
`WorkOrder` (header fields + line items, auto-detecting `tender_type` = `tier_3`
vs `support_engineer` vs `gis`). Most work orders are **digital text PDFs**, so the shared
`run_workorder` pipeline prefers **text** (`pdftotext -layout`, cheaper + exact)
and falls back to **page images** only when there's little/no text (a scan).

## 6c. The reliability layer — `reconcile_workorder`

This is the answer to "don't just trust the LLM on the numbers." After the model
returns, deterministic rules fix the fields that *can* be derived — turning a
model that's right *most* of the time into output that's *consistently* right:

1. **`designation_level`** = the N in "Level N" in the description (not a separate
   guess).
1b. **`tender_type`** = re-derived from the line-item descriptions/HSN — `gis`
   ("GIS Digitization" / HSN 998319) → `tier_3` ("Tier 3" / HSN 998314 / "(Tier-3)"
   tender) → `support_engineer` ("Software Application Support Engineer" / HSN
   998313). The category has an unambiguous signature, so we don't rely on the
   model labelling it right (especially on scans).
2. **Level from `unit_rate` ordering** — within a work order, the rate rises with
   level, and the rate is read *reliably* even when a scanned digit isn't. So a
   level that's inconsistent with where its rate sits among the rows is corrected
   to the unique consistent value (fixed M2601875's blurry "Level 3" read as "5").
3. **`unit_rate`** — `line_total = manpower × period × unit_rate`; when the line
   totals are trustworthy (they sum to the grand total) a row's outlier rate is
   recomputed from its line total.
4. **`taxable_amount`** = the rounded sum of the line totals (not an independent
   read).
5. **Scanned-doc ensemble** — for the image path only, `run_workorder` extracts
   `WORKORDER_SCAN_RUNS` times (default 3) and **majority-votes** each field, to
   absorb OCR variance on degraded scans, *then* reconciles.

Each rule is guarded so it can't over-correct (single-item/fractional-period work
orders are left alone; the level fix needs ≥3 rows sharing one increment and a
unique bound).

## 6d. The other engines — Gemini & local Ollama

The same two jobs, different model — selected by **endpoint**, not by config:

- **`app/gemini.py`** — `/extract-grouped-gemini` and `/extract-workorder-gemini`
  via `langchain-google-genai`. Reuses the *same* prompts, schemas, merge, and
  `run_workorder` reconciliation — only the LLM differs. `gemini-3.5-flash` matches
  100% (incl. the hard multi-month MPR); flash-lite is cheaper but weaker on it.
- **`app/mpr_local.py`** (MPR) & **`app/workorder_local.py`** (work order) — local
  **Ollama**, free + private. Notes: vision models return empty under Ollama's
  `format=json`, so we ask for JSON in the prompt and parse it; `num_ctx` is sized
  to the page count; the local work-order model is **text-only** (can't read scans).
  Fast on a GPU box, slow on the CPU server (~minutes/doc).

---

## 7. Cost knobs (`.env`)

| Knob | Effect |
|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` (accurate) ↔ `claude-haiku-4-5-20251001` (cheapest, ~3× less) — **biggest lever** |
| `PDF_DPI` / `IMAGE_MAX_EDGE` | control image resolution → image tokens (the dominant cost) |
| `ENABLE_PROMPT_CACHE` | caches the static system prompt — small here (images are unique and uncacheable; prompt is below the cache minimum until it grows) |
| `batch_extract.py` | the **Batch API (-50%)** — the real bulk saver, see §8 |

See the cost analysis in `README.md`.

---

## 8. Bulk path — `batch_extract.py`

For the monthly run, this submits **all** PDFs in a folder as one async
**Message Batch** (50% cheaper) using the `anthropic` SDK directly (it shares
`SYSTEM_PROMPT`, the schema, and `_downscale` from `app/`):

```bash
python batch_extract.py <input_folder> <output_folder>
```

It polls until the batch ends, then writes one `<name>.json` per PDF. Async, so
it's for the scheduled job — not real-time. The live endpoint stays for one-offs.

---

## 9. How to trace it yourself

```bash
# what Claude returns for a PDF (live):
curl -F file=@../samples/file.pdf https://pdfparser.aeologic.in/extract-grouped

# locally, with a free port + your key in .env:
python -m app.main           # then open http://localhost:8001/docs
```

Read `extractor.py` top-to-bottom: it's ~100 lines, and the prompt (§4) is where
to look first when the output is wrong.

---

## 10. File reference

| File | Key functions | Notes |
|---|---|---|
| `app/main.py` | the 6 `*_endpoint`s, `health`, `require_api_key`, `_validate_upload`, `run` | FastAPI + auth + Swagger; threadpool offload. |
| `app/extractor.py` | `extract_grouped`, `load_page_images`, `_pdf_to_image_blocks`, `_downscale`, `_merge_by_work_order_month`, `SYSTEM_PROMPT` | MPR-via-Claude pipeline + domain rules + PDF/image loading. |
| `app/workorder.py` | `run_workorder`, `reconcile_workorder`, `_fix_levels_by_rate`, `_vote_workorders`, `extract_workorder`, `SYSTEM_PROMPT` | Work-order pipeline, reliability layers, scanned-doc ensemble. |
| `app/gemini.py` | `extract_grouped_gemini`, `extract_workorder_gemini` | Same jobs via Google Gemini. |
| `app/mpr_local.py` / `app/workorder_local.py` | `extract_grouped_vision` / `extract_workorder_local` | Same jobs via local Ollama. |
| `app/schemas.py` | `Employee`, `MPRRecord`, `MPRDocument`, `WorkOrder`, `WorkOrderItem` | Output shapes = the models' tool schemas. |
| `app/config.py` | `Settings` | `.env` → typed settings (Anthropic, Gemini, Ollama, auth, ensemble). |
| `batch_extract.py` | `_build_request`, `_records_from_message`, `main` | Batch API (-50%) bulk MPR run. |
| `Dockerfile` / `docker-compose.yml` | — | python:3.12-slim + poppler; host 8080; `app/` bind-mounted; autoheal. `.env` change → `docker compose up -d`. |
