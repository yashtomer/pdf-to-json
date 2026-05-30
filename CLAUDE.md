# pdf-to-json — project context for Claude Code

## What this is

A single HTTP API that extracts **NICSI MPR (Monthly Performance Report) PDFs**
into structured JSON, using **Surya 2** (a 650M vision-language OCR model). It
reads even badly-scanned PDFs that Tesseract fails on.

Everything lives in **`surya_extractor/`** (API-only, no web UI). The repo also
has `samples/` (test PDFs, gitignored) and this file.

## The one endpoint that matters

```
POST /extract-grouped   (multipart: file=<pdf>, dpi=150)
→ [ { "work_order": "M2602757",
      "mpr_month": "April 2026",
      "employees": [ { "employee_name": "...", "designation": "...", "leaves": 0 }, ... ] }, ... ]
```

Also: `POST /extract` (raw per-page `blocks` with `html`), `GET /health`, `GET /docs` (Swagger).

## Files (in surya_extractor/)

| File | Role |
|---|---|
| `server.py` | FastAPI app + routes. Loads the model once via lifespan. |
| `extractor.py` | `SuryaExtractor`: `SuryaInferenceManager()` + `RecognitionPredictor` → full-page OCR returning `blocks` (each with a layout `label` + content as `html`; tables as `<table>`). Includes an idempotent `shutil.move` monkey-patch for Surya's model downloader. |
| `mpr_grouper.py` | `group_mpr()`: parses the `<table>` HTML (stdlib `html.parser`, `<br>`→space) into the grouped shape. **Content-driven** column mapping (NICSI tables use a two-row colspan header so header columns don't align with data columns). `leaves` = rightmost cell, parsed safely (dates/"-" → 0). Roster reconciliation fills OCR gaps across months of the same work order. |
| `Dockerfile` | Multi-stage: copies `llama-server` from `ghcr.io/ggml-org/llama.cpp:server`; adds `libssl3`/`libgomp1`; `SURYA_INFERENCE_BACKEND=llamacpp`, `SURYA_INFERENCE_PARALLEL=1`. |
| `docker-compose.yml` | Host port = `${SURYA_HOST_PORT:-8000}`, bound to `127.0.0.1`. `restart: unless-stopped`, named volume `surya-cache` for the GGUF model, healthcheck. |

## How to run (Docker — the supported path)

```bash
cd surya_extractor
docker compose up -d --build            # build + run (8000 by default)
SURYA_HOST_PORT=8080 docker compose up -d --build   # if 8000 is taken
docker compose logs -f                  # wait for "[surya2] Ready."
curl -s http://127.0.0.1:${PORT}/health # {"status":"ok","model_loaded":true}
```

First `/extract*` call downloads the ~1–2 GB GGUF model (HF
`datalab-to/surya-ocr-2-gguf`) into the `surya-cache` volume — slow once, then cached.

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
- **Reverse proxy MUST use long timeouts** (`proxy_read_timeout 900s`) — pages take
  minutes on CPU, so a default 60s nginx timeout returns 504 mid-request.
- Accuracy is excellent on any hardware — Surya 2 reads degraded scans Tesseract
  returns as garbage (verified: it read a scanned April MPR page perfectly).

## Current deployment state (Hostinger VPS)

- Server: Hostinger VPS, Ubuntu, 8 vCPU / 31 GB RAM / 266 GB free, Docker 29.4.2.
- SSH alias on the dev Mac: `hst` → `ssh aeo@187.127.159.226`.
- Repo cloned at `~/pdf-to-json`. Image built. Container runs on **host port 8080**
  (8000 was taken by another app, `invoice-agent`).
- Bound to `127.0.0.1:8080` (localhost only). `curl http://127.0.0.1:8080/health`
  works *on the server*.
- **TODO: external access** — set up the nginx reverse proxy (port 80 →
  `127.0.0.1:8080`, with the 900s timeouts), open the OS firewall (`ufw allow 80`),
  AND open the port in Hostinger's panel-level firewall (hPanel → VPS → Firewall),
  which is separate from `ufw`. See the "Deploy on a Hostinger VPS" section in
  `surya_extractor/README.md` for the exact commands.
- No GPU on Hostinger → expect ~2.5–4 min/page there (fine for low volume).

## Git

- Remote: https://github.com/yashtomer/pdf-to-json (branch `main`).
- End commit messages with the Co-Authored-By trailer. Commit/push only when asked.
