# Run the Surya 2 PDF extractor as an HTTP service.
#   docker build -t surya-extractor .
#   docker run -p 8000:8000 -v surya-cache:/root/.cache surya-extractor
#
# Surya 2 is a 650M VLM served through llama.cpp's `llama-server` (CPU) or
# vLLM (GPU). We copy the prebuilt `llama-server` (+ its .so libs) from the
# official llama.cpp image. The named volume on /root/.cache persists the
# downloaded GGUF model + HF cache between restarts.

# --- llama.cpp server binary (multimodal-capable) -------------------------
FROM ghcr.io/ggml-org/llama.cpp:server AS llama

# --- application image -----------------------------------------------------
FROM python:3.12-slim

# System deps:
#   poppler-utils  - pdf2image
#   libgl1/glib    - opencv (pulled in by surya)
#   libgomp1       - ggml CPU OpenMP (llama-server)
#   libssl3        - llama-server links libssl.so.3 / libcrypto.so.3
#   curl           - healthcheck/debug
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libssl3 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# llama-server + its shared libs live together in /app in the official image.
COPY --from=llama /app /opt/llama/
ENV PATH="/opt/llama:${PATH}" \
    LD_LIBRARY_PATH="/opt/llama:${LD_LIBRARY_PATH}"

# Surya 2 runtime config for CPU operation.
ENV SURYA_INFERENCE_BACKEND=llamacpp \
    SURYA_INFERENCE_PARALLEL=1 \
    SURYA_INFERENCE_STARTUP_TIMEOUT=900 \
    SURYA_INFERENCE_TIMEOUT_SECONDS=900

RUN pip install --no-cache-dir uv

WORKDIR /app

# Step 1: dependencies only (cached unless pyproject/README change)
COPY pyproject.toml README.md ./
RUN uv sync --no-install-project --no-dev

# Step 2: source + project install
COPY server.py extractor.py mpr_grouper.py ./
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
