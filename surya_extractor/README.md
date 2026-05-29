# surya_extractor

A standalone HTTP server that uses **[Surya 2](https://github.com/datalab-to/surya)** (surya-ocr ≥ 0.20) to extract NICSI MPR data from PDFs. It OCRs each page with Surya's 650M vision-language model and returns the grouped employee JSON.

**This is a separate project** — it lives in `surya_extractor/` and does NOT modify the main `pdf_reader.py`. Run it independently (Docker).

## What it gives you

This is an **API-only** service (no web UI).

- 🎯 **`POST /extract-grouped`** — upload a PDF, get the clean grouped shape:
  `[{work_order, mpr_month, employees:[{employee_name, designation, leaves}]}]`
- 🧩 `POST /extract` — raw per-page `blocks` (each with layout `label` + `html`)
- 📖 Auto-generated OpenAPI docs at `/docs`

> **Surya 2 is a 650M generative VLM.** It reads degraded scans far better than
> Tesseract (it correctly read pages Tesseract returned as garbage), but on
> **CPU it is slow — ~2.5–4 min/page**. The speed payoff (~1–2 s/page) only
> appears on a GPU (vLLM) or Apple Silicon. Accuracy is excellent on any
> hardware.

---

## Run it (Docker — required on Intel Mac)

Surya 2 needs a `llama-server` (llama.cpp) backend, which the Docker image
bundles. PyTorch has no Intel-Mac wheels, so Docker is the path here.

```bash
cd surya_extractor
docker build -t surya-extractor .
docker run -d --name surya -p 8000:8000 -v surya-cache:/root/.cache surya-extractor
docker logs -f surya            # wait for "[surya2] Ready."
```

Then POST a PDF to **http://localhost:8000/extract-grouped** (see API below),
or open **http://localhost:8000/docs** for the interactive Swagger UI.

> Use a **named volume** (`surya-cache:/root/.cache`), not a bind mount — a
> macOS Docker bind-mount bug causes spurious ENOSPC errors. The first
> `/extract*` request downloads the GGUF model (~1 GB) and spawns llama-server;
> later requests skip that.

On a GPU Linux host, add `--gpus all` and set `SURYA_INFERENCE_BACKEND=vllm`
for the big speedup. On Apple Silicon you can also run natively (`uv sync` +
`brew install llama.cpp`).

## Deploying on a server (for the team)

### Option A — bare-metal / VM

On any Linux box (Ubuntu / Debian / RHEL):

```bash
# Once
sudo apt install -y python3.12 python3-pip poppler-utils
pip install uv
git clone <your-repo>
cd <repo>/surya_extractor
uv sync

# Run as a background service (simplest: tmux / screen / nohup)
nohup uv run surya-server > server.log 2>&1 &
```

Now anyone on your network can hit `http://<server-ip>:8000`.

For production, put it behind nginx or Caddy and use a process supervisor:

```ini
# /etc/systemd/system/surya-extractor.service
[Unit]
Description=Surya PDF Table Extractor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/surya_extractor
ExecStart=/usr/local/bin/uv run surya-server
Restart=on-failure
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now surya-extractor
```

### Option B — Docker (recommended)

```bash
docker build -t surya-extractor .
docker run -d --name surya -p 8000:8000 -v $(pwd)/.cache:/root/.cache surya-extractor
```

The `-v` volume persists the downloaded model weights between restarts (otherwise the container re-downloads ~1 GB every restart).

For GPU acceleration on NVIDIA hosts:

```bash
docker run -d --name surya --gpus all -p 8000:8000 \
    -v $(pwd)/.cache:/root/.cache \
    -e TORCH_DEVICE=cuda \
    surya-extractor
```

### Option C — Docker Compose

```yaml
services:
  surya:
    build: .
    ports: ["8000:8000"]
    volumes: ["./.cache:/root/.cache"]
    restart: unless-stopped
    # For GPU:
    # deploy:
    #   resources:
    #     reservations:
    #       devices: [{driver: nvidia, count: all, capabilities: [gpu]}]
```

---

## How the team uses it

```bash
curl -F file=@some-mpr.pdf -F dpi=150 \
     http://<server>:8000/extract-grouped \
     -o result.json
```

Then compare `result.json` against the source PDF. Things to check:

| What to look at | Why |
|---|---|
| Employee count vs the PDF | Did every employee get a record? |
| Names / designations exact match | Read correctly (incl. degraded scans)? |
| `leaves` value | Pulled from the Absent column, not a date? |
| `work_order` / `mpr_month` | Correct per page? |
| Mixed-language pages (English + Hindi/Gujarati) | Surya 2 covers 91 languages |

---

## HTTP API

### `POST /extract-grouped` — the main endpoint

Upload an MPR PDF; receive the grouped employee JSON.

```bash
curl -F file=@samples/file.pdf -F dpi=150 \
     http://localhost:8000/extract-grouped \
     -o file.grouped.json
```

Response shape:

```json
[
  {
    "work_order": "M2602757",
    "mpr_month": "April 2026",
    "employees": [
      {
        "employee_name": "Ch. Kiran",
        "designation": "Software Application Support Engineer (4 to less than 6 years relevant experience) - 3rd year 2nd Increment",
        "leaves": 0
      },
      { "employee_name": "K Vijay", "designation": "...", "leaves": 0 }
    ]
  }
]
```

### `POST /extract` — raw blocks

Per-page Surya 2 blocks (each with a layout `label` + content as `html`;
tables as `<table>`). Useful for debugging what the model saw.

### `GET /health`

```json
{ "status": "ok", "model_loaded": true }
```

### `GET /docs`

Auto-generated Swagger UI.

---

## How it works (under the hood)

For every PDF page:

```
1. pdf2image          → render page to a PIL image at the given DPI
2. SuryaInferenceMgr  → (first call) spawn llama-server, load the 650M GGUF VLM
3. RecognitionPredictor([image]) → ONE VLM call returns `blocks`, each with a
                        layout label + content as HTML (tables as <table>)
4. mpr_grouper        → parse the <table> HTML (content-driven column mapping),
                        pull work_order + month from text blocks, emit the
                        grouped {work_order, mpr_month, employees[]} shape
   (legacy v1 path — surya.layout + table_rec + bbox clustering — was replaced
    by the single-VLM full-page call in the Surya 2 migration)
4. Return rows + cell text as JSON
```

Surya 2 is a single 650M-param vision-language model (GGUF, downloaded once
from HF `datalab-to/surya-ocr-2-gguf` and cached in the volume). It's served
through `llama-server` (CPU) or vLLM (GPU); the `SuryaInferenceManager`
spawns/attaches to that backend automatically.

---

## Hardware

| Setup | Performance |
|---|---|
| Laptop CPU (no GPU) | ~3-5 s per page |
| Apple M-series (MPS) | ~1-2 s per page (set `TORCH_DEVICE=mps`) |
| NVIDIA RTX 3060 / 4060+ | ~0.3-0.5 s per page (set `TORCH_DEVICE=cuda`) |
| Cloud GPU (A10/A100) | ~0.1-0.3 s per page |

For team-evaluation purposes, CPU is fine. For production volume, get a GPU.

---

## License notes

- **Surya itself** is GPL-3.0 (commercial license available from [Datalab](https://datalab.to))
- **This wrapper code** in `surya_extractor/` is in the same repo as the rest of `pdf-reader` and inherits its license

For internal team use this is fine. If you ever ship this as a customer product, look at Datalab's commercial licensing terms or swap to PaddleOCR (Apache 2.0).
