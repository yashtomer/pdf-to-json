# LangChain MPR extractor (Claude). Lightweight — no GPU, no model weights.
FROM python:3.12-slim

# poppler-utils → pdf2image; curl → healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY batch_extract.py .

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
