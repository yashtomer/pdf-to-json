# langchain-pdf — MPR extractor via Claude (LangChain)

Upload a NICSI MPR PDF, get the grouped JSON
`[{work_order, mpr_month, employees:[{employee_name, designation, leaves}]}]`.

> 📖 **New here? Read [ARCHITECTURE.md](ARCHITECTURE.md)** — the full request flow,
> the prompt rules, the schemas, cost knobs, and the batch path.

Claude reads the page images and returns the structured result directly (via
LangChain + Anthropic tool-calling) — **no OCR engine and no GPU**. The MPR-format
rules we learned (per-row work orders, leaves vs. remarks, multi-month splits,
grouped name cells, continuation pages…) live in the prompt, so new layouts mostly
just work instead of needing code changes.

## Setup

```bash
cd langchain-pdf

# 1. system dependency for pdf2image
brew install poppler            # macOS    (Linux: sudo apt install poppler-utils)

# 2. python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. configure
cp .env.example .env            # then paste your key into ANTHROPIC_API_KEY
```

## Run

```bash
source .venv/bin/activate
python -m app.main              # starts on http://localhost:8001
# or: uvicorn app.main:app --host 0.0.0.0 --port 8001
```

- **Swagger UI:** http://localhost:8001/docs
- **ReDoc:** http://localhost:8001/redoc
- **OpenAPI JSON:** http://localhost:8001/openapi.json

## Use

```bash
curl -X POST -F file=@../samples/file.pdf \
     http://localhost:8001/extract-grouped -o result.json
```

Response:

```json
[
  {
    "work_order": "M2602757",
    "mpr_month": "April 2026",
    "employees": [
      {"employee_name": "Ch. Kiran", "designation": "Software Application Support Engineer ...", "leaves": 0}
    ]
  }
]
```

## Endpoints

| Endpoint | Input | Output |
|---|---|---|
| `POST /extract-grouped` | an MPR PDF | `[{work_order, mpr_month, employees[]}]` |
| `POST /extract-workorder` | a NICSI **Work Order** PDF | structured work-order fields + line items |
| `GET /health` | — | `{status, model, api_key_configured, auth_enabled}` |

Both extraction endpoints need the `X-API-Key` header (see Authentication).

### Work Order (`/extract-workorder`)
Parses a NICSI Work Order into fields + line items, **auto-detecting `tender_type`**:
- `tier_3` — items are "Level N … Tier 3" (HSN 998314, empanelment no. has "(Tier-3)");
  `designation_level` = the N.
- `support_engineer` — items are "Software Application Support Engineer …" (HSN
  998313); `designation_level` = null.

Work orders are text PDFs, so it extracts text with `pdftotext` (cheaper/accurate;
falls back to images for scans).

```bash
curl -X POST -H "X-API-Key: <key>" -F file=@M2511251.pdf \
     https://pdfparser.aeologic.in/extract-workorder
# → { work_order_number, project_number, project_name, date_issued, tender_number,
#     tender_type, user_contact_detail, wo_total_value, taxable_amount,
#     items: [{ line_no, hsn_code, description, designation_level, manpower_count,
#               period_text, start_date, end_date, unit_rate, taxable_amount, line_total }] }
```

## Cost optimizations (built in)

| Lever | How | Impact |
|---|---|---|
| **Model choice** | `ANTHROPIC_MODEL` — Sonnet (accurate) vs `claude-haiku-4-5-20251001` (cheapest) | **~3×** — biggest lever |
| **Batch API −50%** | `python batch_extract.py <folder>` for bulk monthly runs (async) | **half price** on every token |
| **Image size** | `IMAGE_MAX_EDGE` caps resolution + pages sent as JPEG | fewer image tokens (the dominant cost) + far smaller uploads |
| **`PDF_DPI`** | lower DPI = fewer tokens | tune per accuracy need |
| **Prompt caching** | `ENABLE_PROMPT_CACHE` marks the system prompt with `cache_control` | small here — see note |

> **Honest note on caching:** the page *images* are unique per document and can't
> be cached; only the static system prompt can. Anthropic also only caches a
> prefix ≥1024 tokens, and the rules prompt is shorter than that today — so
> caching is effectively a no-op until the prompt grows (e.g. adding few-shot
> examples). The structure is correct and free; the real savers for this workload
> are **model choice** and the **Batch API**.

**Bulk (recommended for the monthly run):**
```bash
python batch_extract.py ../samples out/   # submits all PDFs as one -50% batch
```
At ~550 MPRs/month this is roughly **~$4/mo on Sonnet, ~$1.50/mo on Haiku**.

## Authentication

`/extract-grouped` is protected by an **API key** so the public URL can't be
abused (it spends your Anthropic budget). Callers send it as the `X-API-Key`
header. `/health` and `/docs` stay open.

```bash
# 1. generate a strong key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. put it in .env (comma-separate several to allow multiple callers)
#    API_AUTH_KEYS=<key1>,<key2>
docker compose up -d            # up -d (not restart) so .env is re-read

# 3. callers include the header
curl -X POST -H "X-API-Key: <key>" -F file=@mpr.pdf \
     https://pdfparser.aeologic.in/extract-grouped
```

- Missing/invalid key → **401**.
- `API_AUTH_KEYS` empty → **auth disabled** (open) — the service still runs, so
  set a key to actually lock it down. `GET /health` reports `auth_enabled`.
- In Swagger (`/docs`) click **Authorize** and paste a key to call it from the UI.

> For internet-facing payroll data, also keep TLS on (already via Apache) and
> rotate keys periodically.

## Configuration (`.env`)

| Key | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | your key (`sk-ant-...`) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | `claude-haiku-4-5-20251001` is cheaper |
| `ANTHROPIC_MAX_TOKENS` | `4096` | output cap |
| `ANTHROPIC_TIMEOUT` | `180` | seconds |
| `PDF_DPI` | `150` | render resolution |
| `MAX_PAGES` | `25` | pages sent per document |
| `IMAGE_MAX_EDGE` | `1568` | cap long edge (px); lower = cheaper |
| `ENABLE_PROMPT_CACHE` | `true` | cache the static system prompt |
| `API_AUTH_KEYS` | _(empty)_ | comma-separated keys for `X-API-Key`; empty = open |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8001` | server bind |

## Layout

```
langchain-pdf/
├── app/
│   ├── config.py      # .env -> typed settings
│   ├── schemas.py     # Pydantic models (also guide Claude's output)
│   ├── extractor.py   # PDF -> images -> Claude -> records
│   └── main.py        # FastAPI app + Swagger
├── batch_extract.py   # bulk run via Message Batches API (-50%)
├── .env.example
├── requirements.txt
└── README.md
```
