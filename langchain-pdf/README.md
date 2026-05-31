# langchain-pdf — MPR extractor via Claude (LangChain)

Upload a NICSI MPR PDF, get the grouped JSON
`[{work_order, mpr_month, employees:[{employee_name, designation, leaves}]}]`.

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
