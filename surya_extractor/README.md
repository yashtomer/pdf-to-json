# surya_extractor

> **Status: fallback.** Since 2026-05-31 the live service at
> `https://pdfparser.aeologic.in` is **`../langchain-pdf`** (Claude). This Surya
> service is the fully-on-prem fallback (no data leaves the box, but slow on CPU).
> It's stopped on the server — to run it again, use a port other than 8080.

A standalone HTTP API that uses **[Surya 2](https://github.com/datalab-to/surya)** (surya-ocr ≥ 0.20) to extract NICSI MPR data from PDFs. It OCRs each page with Surya's 650M vision-language model and returns the grouped employee JSON.

Runs as a Docker container (or natively on Apple Silicon). CPU works anywhere; a GPU host makes it ~100× faster (see [Deploy on a server](#deploy-on-a-server)).

> 📖 **New here? Read [ARCHITECTURE.md](ARCHITECTURE.md)** — a plain-language walkthrough of the code and the full PDF→JSON flow.

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

## Pick your platform

| You're on… | Go to | Speed |
|---|---|---|
| A Linux server (production) | [Deploy on a Hostinger VPS](#deploy-on-a-hostinger-vps) — the live deploy, or [Deploy on a server](#deploy-on-a-server) (generic CPU/GPU) | ~2.5–4 min/page CPU · ~1–2 s GPU |
| A MacBook with **Apple Silicon (M1/M2/M3)** | [Run on a MacBook](#run-on-a-macbook-apple-silicon) — native Metal is fastest | ~5–15 s/page |
| An **Intel Mac** | [Run on an Intel Mac](#run-on-an-intel-mac-docker) — Docker required | ~2.5–4 min/page |

---

## Deploy on a Hostinger VPS

> ✅ **Already live:** this service is deployed at **<https://pdfparser.aeologic.in>**
> (`/health`, `/docs`, `/extract-grouped`). It runs in Docker on host port `8080`
> behind an **Apache** reverse proxy with a Let's Encrypt cert. The steps below
> document that setup. **This VPS uses Apache, not nginx** (it already fronts ~20
> other `*.aeologic.in` vhosts), so step 6 shows the Apache vhost actually in use.

Hostinger **VPS** plans run a normal Linux server you fully control, so Docker
works. (Hostinger *Shared* / *Web* hosting will **not** work — no Docker, no
root. You need a **VPS** plan.)

> ⚠️ **No GPU on Hostinger VPS** → it runs the CPU path: **~2.5–4 min per page**.
> That's fine for low volume. Pick a plan with enough RAM — **≥ 8 GB** (KVM 2 or
> higher). The image (~9 GB) + model (~1–2 GB) also need **~15 GB free disk**.

### 1. Create / pick the VPS

In hPanel: **VPS → choose a plan with ≥ 8 GB RAM** (KVM 2+), OS template
**Ubuntu 24.04** (or "Ubuntu 24.04 with Docker" if offered — skips step 3).

### 2. SSH into it

From hPanel copy the server IP + root password, then on your Mac:

```bash
ssh root@YOUR_SERVER_IP
```

### 3. Install Docker (skip if you chose the Docker OS template)

```bash
apt update && apt install -y docker.io docker-compose-plugin git
systemctl enable --now docker
docker --version          # confirm it works
```

### 4. Get the code + build + run

```bash
git clone https://github.com/yashtomer/pdf-to-json.git
cd pdf-to-json/surya_extractor
docker compose up -d --build      # builds the image, then starts it
docker compose logs -f            # wait for "[surya2] Ready." then Ctrl-C
```

> **Port already in use?** If another app on the server already listens on 8000,
> the container fails with *"address already in use."* The compose host port is
> configurable via `SURYA_HOST_PORT` — pick a free one:
> ```bash
> sudo ss -tlnp | grep -E ':(8000|8080|8001)' || echo "free"   # check first
> SURYA_HOST_PORT=8080 docker compose up -d --build            # use, e.g., 8080
> ```
> Wherever the steps below say `8000`, use your chosen port (e.g. `8080`) —
> including the proxy target in step 6. (Prod uses `8080`.)

> The `docker-compose.yml` binds to `127.0.0.1` (localhost only) for safety. To
> reach it from outside, use the reverse proxy in step 6 (recommended), or
> change the compose bind to `0.0.0.0:${SURYA_HOST_PORT:-8000}:8000` and open
> the firewall (step 5).

### 5. Open the firewall (only if exposing 8000 directly)

```bash
ufw allow 8000/tcp        # skip this if you use the reverse proxy in step 6
```

### 6. Put a reverse proxy in front (recommended — TLS + the timeout fix)

Pages take minutes on CPU, so the **long-timeout config is mandatory** or you'll
get 504s.

**Apache — what this VPS actually uses.** The box already runs Apache2 fronting
the other `*.aeologic.in` sites, so surya is just another vhost. This is the live
config (`/etc/apache2/sites-available/pdfparser.aeologic.in.conf`):

```apache
<VirtualHost *:80>
    ServerName pdfparser.aeologic.in
    ProxyPreserveHost On
    ProxyPass / http://localhost:8080/ timeout=900
    ProxyPassReverse / http://localhost:8080/
    RequestHeader set X-Forwarded-Proto expr=%{REQUEST_SCHEME}

    Timeout 900                  # ← critical: allow multi-minute CPU requests
    ProxyTimeout 900
    LimitRequestBody 0           # allow large PDF uploads (0 = unlimited)

    ErrorLog  ${APACHE_LOG_DIR}/pdfparser.aeologic.in-error.log
    CustomLog ${APACHE_LOG_DIR}/pdfparser.aeologic.in-access.log combined
</VirtualHost>
```

```bash
a2enmod proxy proxy_http ssl headers rewrite
a2ensite pdfparser.aeologic.in
apache2ctl configtest && systemctl reload apache2
```

For HTTPS, point the domain's A-record at the VPS IP, then let certbot add the
`:443` vhost (`-le-ssl.conf`) and the HTTP→HTTPS redirect automatically:

```bash
apt install -y certbot python3-certbot-apache
certbot --apache -d pdfparser.aeologic.in   # free Let's Encrypt cert + auto-renew
```

**nginx — alternative for a fresh box without Apache:**

```bash
apt install -y nginx
cat >/etc/nginx/sites-available/surya <<'NGINX'
server {
    listen 80;
    server_name YOUR_DOMAIN_OR_IP;

    location / {
        proxy_pass http://127.0.0.1:8080;   # match SURYA_HOST_PORT
        proxy_read_timeout   900s;     # ← critical: allow multi-minute requests
        proxy_send_timeout   900s;
        client_max_body_size 50m;      # allow large PDF uploads
    }
}
NGINX
ln -s /etc/nginx/sites-available/surya /etc/nginx/sites-enabled/surya
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
ufw allow 80/tcp
# then: apt install -y certbot python3-certbot-nginx && certbot --nginx -d YOUR_DOMAIN
```

### 7. Warm it up + test

```bash
# first call downloads the ~1-2 GB model — do it once after deploy
curl -F file=@~/pdf-to-json/samples/file.pdf -F dpi=150 \
     http://127.0.0.1:8080/extract-grouped -o /dev/null

# from your own machine, through the proxy (the live deploy):
curl -F file=@some-mpr.pdf -F dpi=150 \
     https://pdfparser.aeologic.in/extract-grouped -o result.json
```

### 8. Day-to-day ops on the VPS

```bash
cd ~/pdf-to-json/surya_extractor
docker compose ps            # status
docker compose logs -f       # live logs
docker compose restart       # restart (model reloads from volume, ~10s)
docker compose down          # stop
```

**Deploying updates — no rebuild for code changes.** The source `.py` files are
bind-mounted into the container (see `docker-compose.yml`), so:

```bash
# code change (server.py / extractor.py / mpr_grouper.py):
git pull && docker compose restart            # ~10s, NO rebuild

# dependency change (pyproject.toml / Dockerfile):
git pull && docker compose up -d --build      # full rebuild (~5-10 min)
```

`docker compose restart` re-imports the updated code and reloads the model from
the cache volume (~10s). Only a `pyproject.toml`/`Dockerfile` change needs the
slow `--build`.

The container has `restart: unless-stopped`, so it comes back automatically
after a reboot or crash.

### Hostinger gotchas

| Symptom | Cause / fix |
|---|---|
| `504 Gateway Timeout` | proxy default 60s timeout — apply the long-timeout config in step 6 (Apache `Timeout 900`/`ProxyTimeout 900`, or nginx `proxy_read_timeout 900s`) |
| Build killed / OOM | < 8 GB RAM — upgrade the plan, or add swap: `fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile` |
| `no space left on device` | image+model need ~15 GB — pick a bigger disk or prune: `docker system prune -af` |
| Can't connect on port 8000/8080 | it's bound to localhost — use the reverse proxy (step 6) or change the compose `ports:` mapping |
| Slow (~3 min/page) | expected — Hostinger VPS has no GPU. For speed you'd need a GPU host (not Hostinger) |

---

## Run on a MacBook (Apple Silicon)

On Apple Silicon (M1/M2/M3) you have two options. **Native is the fast path** —
llama.cpp offloads to the Metal GPU (~5–15 s/page). Docker is the simplest but
runs CPU-only (~16 s/page).

### Native (recommended on M1/M2/M3 — Metal acceleration)

Surya 2 needs a `llama-server` binary on PATH; on macOS the easiest source is
Homebrew. Then run the FastAPI app with [uv](https://docs.astral.sh/uv/):

```bash
brew install llama.cpp uv poppler    # llama-server (Metal) · uv · poppler (for pdf2image)
cd surya_extractor
uv run uvicorn server:app --host 127.0.0.1 --port 8000
```

The first `/extract*` call downloads the ~1–2 GB GGUF model and spawns
`llama-server`; on Apple Silicon llama.cpp uses the Metal GPU automatically.
Then POST to **http://localhost:8000/extract-grouped** or open
**http://localhost:8000/docs**.

> `SURYA_INFERENCE_BACKEND=llamacpp` is the default. The model is cached under
> `~/.cache` so it only downloads once.

### Docker (simplest, CPU-only)

The image is multi-arch, so it runs **natively on arm64** (no slow x86 emulation)
— just without Metal. Use the [Intel Mac steps](#run-on-an-intel-mac-docker)
below; they're identical on Apple Silicon.

---

## Run on an Intel Mac (Docker)

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

---

## Deploy on a server

Everything runs in one Docker container. Pick CPU (works on any Linux box) or
GPU (much faster). For a turnkey, copy-paste walkthrough see
[Deploy on a Hostinger VPS](#deploy-on-a-hostinger-vps) above.

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
with default timeouts (60 s) will return **504 Gateway Timeout** mid-extraction.
Raise the timeouts.

**Apache** — what the live deploy uses (see the
[Hostinger section](#deploy-on-a-hostinger-vps) for the full vhost):

```apache
<VirtualHost *:80>
    ServerName mpr.example.com
    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:8080/ timeout=900   # match SURYA_HOST_PORT
    ProxyPassReverse / http://127.0.0.1:8080/

    Timeout 900            # ← critical: allow multi-minute requests
    ProxyTimeout 900
    LimitRequestBody 0     # allow large PDF uploads (0 = unlimited)
</VirtualHost>
```

nginx equivalent (for a fresh box without Apache):

```nginx
server {
    listen 80;
    server_name mpr.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;           # match SURYA_HOST_PORT
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
and let the reverse proxy (Apache in prod; or nginx/Caddy) handle TLS + auth, or
restrict the port with a firewall.

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
