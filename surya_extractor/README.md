# surya_extractor

A standalone HTTP API that uses **[Surya 2](https://github.com/datalab-to/surya)** (surya-ocr ≥ 0.20) to extract NICSI MPR data from PDFs. It OCRs each page with Surya's 650M vision-language model and returns the grouped employee JSON.

Runs as a Docker container. CPU works anywhere; a GPU host makes it ~100× faster (see [Deploy on a server](#deploy-on-a-server)).

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

For a real server (CPU restart-policy, GPU, reverse proxy, compose), see
[Deploy on a server](#deploy-on-a-server) below.

## Deploy on a server

Everything runs in one Docker container. Pick CPU (works on any Linux box) or
GPU (much faster).

### Server requirements

| Resource | CPU deploy | GPU deploy |
|---|---|---|
| OS | Linux + Docker | Linux + Docker + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) |
| RAM | ≥ 6 GB free | ≥ 8 GB |
| Disk | ~10 GB (image ~9 GB + GGUF model ~1-2 GB) | same |
| GPU | — | any NVIDIA card with ≥ 8 GB VRAM |
| Speed | ~2.5–4 min/page | ~1–2 s/page |

### 1. Get the code onto the server

```bash
git clone https://github.com/yashtomer/pdf-to-json.git
cd pdf-to-json/surya_extractor
docker build -t surya-extractor .          # ~5-10 min first build
```

### 2a. Run on CPU (works now, no GPU needed)

```bash
docker run -d --name surya \
    --restart unless-stopped \
    -p 8000:8000 \
    -v surya-cache:/root/.cache \
    surya-extractor
```

- `--restart unless-stopped` → survives reboots / crashes.
- `-v surya-cache:/root/.cache` → a **named volume** persists the ~1-2 GB GGUF
  model so it's downloaded only once (not on every restart).

### 2b. Run on GPU (≈100× faster — recommended for real volume)

The bundled image uses the CPU (llama.cpp) backend. To use a GPU you switch
Surya to the vLLM backend, which needs `vllm` installed in the image. Build the
GPU variant (CUDA base + `pip install vllm`) and run with:

```bash
docker run -d --name surya \
    --gpus all \
    --restart unless-stopped \
    -p 8000:8000 \
    -v surya-cache:/root/.cache \
    -e SURYA_INFERENCE_BACKEND=vllm \
    surya-extractor-gpu
```

> A ready-made GPU Dockerfile isn't included yet — ask and I'll add
> `Dockerfile.gpu` (CUDA base image + vllm). The CPU image above is what ships
> today.

### 3. Reverse proxy — set LONG timeouts ⚠️

This is the #1 server gotcha. Each page takes **minutes on CPU**, so a proxy
with default timeouts (nginx = 60 s) will return **504 Gateway Timeout** mid-
extraction. Raise the timeouts:

```nginx
server {
    listen 80;
    server_name mpr.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_read_timeout   900s;   # ← critical: allow multi-minute requests
        proxy_send_timeout   900s;
        client_max_body_size 50m;    # allow large PDF uploads
    }
}
```

Clients (curl, Postman, the team's scripts) must likewise allow long requests —
e.g. `curl --max-time 1800`.

### 4. First request warms up

The first `/extract*` call downloads the GGUF model and spawns the inference
backend (slow). Pre-warm after deploy so the first real user isn't the one who
waits:

```bash
curl -F file=@samples/file.pdf -F dpi=150 http://localhost:8000/extract-grouped -o /dev/null
```

### 5. Health & ops

```bash
curl http://<server>:8000/health     # {"status":"ok","model_loaded":true}
docker logs -f surya                  # live logs
docker restart surya                  # model reloads from the cache volume (~10 s)
```

### Security

The container binds `0.0.0.0:8000`. For anything internet-facing, **don't
expose 8000 directly** — bind it behind the reverse proxy (`-p 127.0.0.1:8000:8000`)
and let nginx/Caddy handle TLS + auth, or restrict port 8000 with a firewall.

### docker-compose (declarative alternative)

A `docker-compose.yml` is included — on the server just run:

```bash
docker compose up -d
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
```

Surya 2 is a single 650M-param vision-language model (GGUF, downloaded once
from HF `datalab-to/surya-ocr-2-gguf` and cached in the volume). It's served
through `llama-server` (CPU) or vLLM (GPU); the `SuryaInferenceManager`
spawns/attaches to that backend automatically.

---

## Hardware & speed

| Setup | Backend | Speed/page |
|---|---|---|
| Linux/Intel CPU (Docker) | llama.cpp | ~2.5–4 min |
| Apple Silicon (native) | llama.cpp (Metal) | ~5–15 s |
| NVIDIA GPU (≥8 GB) | vLLM | ~1–2 s |

Backend is chosen by `SURYA_INFERENCE_BACKEND` (`llamacpp` default in the CPU
image; `vllm` for the GPU image). `SURYA_INFERENCE_PARALLEL=1` is set for CPU to
avoid OOM — raise it on GPU for higher throughput.

For low-volume / evaluation, CPU is fine. For production throughput, deploy on
a GPU host (see [Deploy on a server](#deploy-on-a-server)).

---

## License notes

- **Surya weights** use a modified OpenRAIL-M license (free for research,
  personal use, and orgs under $5M funding/revenue); broader commercial use →
  [Datalab pricing](https://www.datalab.to/pricing). The Surya **code** is
  Apache 2.0.
- **llama.cpp** (the bundled `llama-server`) is MIT.

Check Datalab's terms before shipping this as a paid customer-facing product.
