# pdf-to-json — project context for Claude Code

## What this is

This repo extracts **NICSI MPR (Monthly Performance Report) PDFs** into
structured JSON. There are **two** extractor services:

- **`langchain-pdf/`** — **THE LIVE SERVICE** (since 2026-05-31). Sends page
  images to **Anthropic Claude** (`claude-sonnet-4-6`) via LangChain and returns
  the grouped JSON directly. No OCR engine, no GPU. Serves
  `https://pdfparser.aeologic.in` now.
- **`surya_extractor/`** — the original, now a **stopped fallback.** Local
  **Surya 2** (650M VLM) OCR + a content-driven Python parser (`mpr_grouper.py`),
  fully on-prem. Slow on CPU; kept for the case data may not leave the box.

We pivoted to Claude because MPR layouts are **dynamic + scanned**: Claude adapts
to new formats without per-format parser code, costs ~$8/mo (Sonnet) for ~550
MPRs/mo, and is ~15 s/doc vs Surya's ~3 min/page on CPU. Verified Claude/Sonnet
matches expected outputs incl. the hard multi-month leave-certificate case
(file17). Also in the repo: `samples/` (test PDFs, gitignored) and this file.

## The one endpoint that matters

Live base URL: **`https://pdfparser.aeologic.in`**. **Both** services expose the
same main endpoint, so callers don't change between them:

```
POST /extract-grouped   (multipart: file=<pdf>)
→ [ { "work_order": "M2602757",
      "mpr_month": "April 2026",
      "employees": [ { "employee_name": "...", "designation": "...", "leaves": 0 }, ... ] }, ... ]
```

**Twelve extraction endpoints** (each accepts a **PDF or image** — jpg/png/…; MPRs
and Form 11s are often phone photos). `-gemini` = Google Gemini, `-groq` = Groq
(Llama 4 Scout, vision), `-with-local-llm` / `-qwen3-vl` = local Ollama; the rest
= Claude. **Every doc type now has a Groq variant** (`-groq`):
- MPR: `POST /extract-grouped` (Claude), `/extract-grouped-gemini`, `/extract-grouped-groq`, `/extract-grouped-qwen3-vl`
- Work Order: `POST /extract-workorder` (Claude), `/extract-workorder-gemini`, `/extract-workorder-groq`, `/extract-workorder-with-local-llm`
  — auto-detects `tender_type` (`tier_3` vs `support_engineer` vs `gis`).
- Payment Advice: `POST /extract-payment-advice` (Claude), `/extract-payment-advice-groq`.
- Form 11 (EPF declaration): `POST /extract-form11` (Claude), `/extract-form11-groq`.

All extraction endpoints require an **`X-API-Key`** header (keys in
`API_AUTH_KEYS`, constant-time compared). Open: `GET /health`, `GET /docs`.
**100% model:** Gemini `gemini-3.5-flash` or Claude Sonnet (flash-lite is cheaper
but ~14/18 on the hardest multi-month MPR). Bulk MPR path:
`langchain-pdf/batch_extract.py` (Anthropic Batch API, -50%). The raw `POST
/extract` (per-page `blocks`+`html`) exists **only on Surya**.

**Work-order reliability layers** (deterministic, after the model — see
`reconcile_workorder`): designation_level ← "Level N" in description;
tender_type ← line-item description/HSN signature (`gis`→`tier_3`→`support_engineer`);
level ← unit_rate ordering (fixes blurry digits); unit_rate ← line-total arithmetic;
taxable_amount ← sum of line totals; scanned docs ← N-run majority vote
(`WORKORDER_SCAN_RUNS`).

## Files (in langchain-pdf/) — the live service

| File | Role |
|---|---|
| `app/main.py` | FastAPI: the 12 extraction endpoints + `/health` + Swagger. `_validate_upload` accepts pdf/jpg/png/…; `require_api_key` enforces `X-API-Key`. Blocking calls run in a threadpool. |
| `app/extractor.py` | MPR via Claude: `load_page_images` (PDF→pdf2image OR image→PIL, by `%PDF-` magic) → downscaled JPEG → one `with_structured_output(MPRDocument)` call. Domain rules in `SYSTEM_PROMPT`. `temperature=0`. `_merge_by_work_order_month` consolidates (work_order, month). |
| `app/workorder.py` | Work Order via Claude: `pdftotext -layout` (image fallback for scans, `WORKORDER_SCAN_RUNS` majority vote) → `WorkOrder`. `reconcile_workorder` = the deterministic reliability layers (level from description + rate-ordering, unit_rate + taxable_amount arithmetic). Shared `run_workorder` used by the Gemini path too. |
| `app/gemini.py` | MPR (vision) + Work Order (text/image) via Google Gemini (langchain-google-genai). |
| `app/groq.py` | MPR (vision) + Work Order + Payment Advice via Groq (langchain-groq, Llama 4 Scout). Reuses each Claude path's prompt + shared pipeline. Uses `with_structured_output(method="json_schema")` — Groq's default tool-calling 400s on a strict type mismatch (e.g. Llama emitting an int field as a string); json_schema constrains the output to the right types. (Form 11's Groq path is in form11.py.) |
| `app/payment_advice.py` | Payment Advice via Claude: `pdftotext -layout` (image fallback) → `PaymentAdvice` (net `pa_amount`, `pa_date`, enclosed `bills[]` mapping `bill_no`→`work_order`). |
| `app/form11.py` | EPF **Form 11** (hand-filled scan) via Claude **or** Groq (Llama 4 Scout vision); same prompt + schema for both. `_normalize` = deterministic clean-up (digits-only IDs, space-free email, PAN-shape corrector that only fixes an invalid PAN). |
| `app/mpr_local.py` | MPR via local Ollama **vision** (qwen3-vl); no `format=json` (returns empty) — JSON parsed from the reply; dynamic `num_ctx` for multi-page. |
| `app/workorder_local.py` | Work Order via local Ollama **text** (qwen2.5:14b); raw `/api/generate` + `num_ctx=16384`. |
| `app/schemas.py` | Pydantic models — also drive structured output. `WorkOrder`/`WorkOrderItem` lenient (defaults + `coerce_numbers_to_str`) for local JSON. |
| `app/config.py` | `.env` → typed settings (Anthropic, Gemini, Groq, Ollama, auth, ensemble, DPI). |
| `batch_extract.py` | Bulk run via Anthropic Message Batches API (**-50%**) for the monthly job. |
| `Dockerfile` / `docker-compose.yml` | python:3.12-slim + poppler; runs on host **8080** (same as Surya); `app/` bind-mounted (no-rebuild code changes); `restart: unless-stopped` + label-scoped `autoheal`. **`.env` changes need `docker compose up -d` (not `restart`).** |

## Files (in surya_extractor/) — the stopped fallback

| File | Role |
|---|---|
| `server.py` | FastAPI app + routes. Loads the model once via lifespan. Single-flight busy-guard (503 when busy) + threadpool OCR. |
| `extractor.py` | `SuryaExtractor`: `SuryaInferenceManager()` + `RecognitionPredictor` → full-page OCR returning `blocks` (each with a layout `label` + content as `html`; tables as `<table>`). Includes an idempotent `shutil.move` monkey-patch for Surya's model downloader. |
| `mpr_grouper.py` | `group_mpr()`: parses the `<table>` HTML (stdlib `html.parser`, `<br>`→space) into the grouped shape. **Content-driven** column mapping (NICSI tables use a two-row colspan header so header columns don't align with data columns). `leaves` = rightmost cell, parsed safely (dates/"-" → 0). Roster reconciliation fills OCR gaps across months of the same work order. |
| `Dockerfile` | Multi-stage: copies `llama-server` from `ghcr.io/ggml-org/llama.cpp:server`; adds `libssl3`/`libgomp1`; `SURYA_INFERENCE_BACKEND=llamacpp`, `SURYA_INFERENCE_PARALLEL=1`. |
| `docker-compose.yml` | Host port = `${SURYA_HOST_PORT:-8000}`, bound to `127.0.0.1`. `restart: unless-stopped`, named volume `surya-cache` for the GGUF model, healthcheck. |

## How to run

**langchain-pdf (the live service):**
```bash
cd langchain-pdf
cp .env.example .env                     # then set ANTHROPIC_API_KEY (clean value, no inline # comment)
docker compose up -d --build             # host 8080; Swagger at /docs
# local dev: pip install -r requirements.txt && python -m app.main  (needs poppler)
```

**surya_extractor (the fallback, Docker):**
```bash
cd surya_extractor
docker compose up -d --build            # build + run (8000 by default)
SURYA_HOST_PORT=8080 docker compose up -d --build   # if 8000 is taken (prod uses 8080)
docker compose logs -f                  # wait for "[surya2] Ready."
curl -s http://127.0.0.1:${PORT}/health # {"status":"ok","model_loaded":true}
```

First `/extract*` call downloads the ~1–2 GB GGUF model (HF
`datalab-to/surya-ocr-2-gguf`) into the `surya-cache` volume — slow once, then cached.

**Deploying code changes needs NO rebuild.** The `.py` source files are
bind-mounted over `/app` (the venv lives at `/opt/venv` so it isn't shadowed;
`PYTHONPATH=/app` lets `import server` resolve). So:
- code edit (server/extractor/mpr_grouper.py): `git pull && docker compose restart` (~10s)
- dependency change (pyproject.toml / Dockerfile): `git pull && docker compose up -d --build`

## Hard-won facts (don't relearn these)

- **Surya 2 = `surya-ocr >= 0.20`.** It dropped the v1 `surya.foundation` API and
  rewrote everything around `SuryaInferenceManager` + a llama.cpp / vLLM backend.
  Don't downgrade or use the old `FoundationPredictor` API.
- **Needs a `llama-server` binary** (from llama.cpp) on PATH — the Docker image
  bundles it. The model runs through that backend.
- **Speed depends entirely on hardware** (it's a generative VLM, CPU-bound without a GPU):
  - x86_64 emulated Docker: ~2.5–4 min/page (avoid)
  - arm64-native Docker (CPU): ~16 s/page
  - native macOS + Metal GPU (`-ngl 99`): ~9 s/page
  - NVIDIA GPU + vLLM: ~1–2 s/page
- **Use a named Docker volume** (`surya-cache:/root/.cache`), not a bind mount —
  a macOS Docker bind-mount bug throws spurious ENOSPC. Named volume avoids it.
- **Reverse proxy MUST use long timeouts** — pages take minutes on CPU, so a
  default 60s timeout returns 504 mid-request. Apache (what prod uses):
  `Timeout 900` + `ProxyTimeout 900` and `ProxyPass ... timeout=900`. nginx
  equivalent: `proxy_read_timeout 900s`.
- Accuracy is excellent on any hardware — Surya 2 reads degraded scans Tesseract
  returns as garbage (verified: it read a scanned April MPR page perfectly).

## Current deployment state (Hostinger VPS) — LIVE

- **`https://pdfparser.aeologic.in` is served by `langchain-pdf` (Claude
  `claude-sonnet-4-6`) since 2026-05-31.** Health →
  `{"status":"ok","model":"...","api_key_configured":true}`. Surya was switched
  off (`docker compose down` in `surya_extractor`) but its image/data remain as a
  fallback — bring it back on a non-8080 port (8080 is now langchain-pdf).
- Server: Hostinger VPS, Ubuntu, 8 vCPU / 31 GB RAM, Docker, IP `187.127.159.226`.
  SSH alias `hst` → `ssh aeo@187.127.159.226` (key-based; **no passwordless sudo**).
  Repo at `~/pdf-to-json`. Docker `enabled` on boot.
- **langchain-pdf** runs in Docker, container 8000 → **host 8080** (`127.0.0.1`),
  `restart: unless-stopped` + `langchain-pdf-autoheal`. Config in
  `langchain-pdf/.env` (gitignored: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`).
  Deploy: `git pull && docker compose restart` for code, **`up -d` for `.env`**.
- **Reverse proxy = Apache (NOT nginx).** Vhost
  `/etc/apache2/sites-available/pdfparser.aeologic.in{,-le-ssl}.conf`:
  `ProxyPass / http://localhost:8080/ timeout=900` + `ProxyPassReverse`,
  `ProxyPreserveHost On`, `Timeout 900`/`ProxyTimeout 900`, `LimitRequestBody 0`,
  and **`RequestReadTimeout header=20-40,MinRate=500 body=0`** (added so large
  >1 MB uploads don't 408). Modules `proxy proxy_http ssl headers reqtimeout`.
  (Editing the vhost needs root, which the dev session does not have.)
- **TLS**: Let's Encrypt at `/etc/letsencrypt/live/pdfparser.aeologic.in/`
  (certbot auto-renew). HTTP→HTTPS redirect in place.
- The long Apache timeouts were the Surya CPU-page fix; langchain-pdf is fast
  (~15 s/doc) so they're now just harmless slack.

## Git

- Remote: https://github.com/yashtomer/pdf-to-json (branch `main`).
- End commit messages with the Co-Authored-By trailer. Commit/push only when asked.
