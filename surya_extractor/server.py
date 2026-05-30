"""
FastAPI server exposing Surya 2 MPR extraction via HTTP. API-only (no web UI).

Endpoints
---------
GET  /                API root — lists the endpoints.
GET  /health          Liveness probe + model-loaded status.
POST /extract         Upload a PDF, receive raw per-page blocks (label + html).
POST /extract-grouped Upload an MPR PDF, receive grouped employee JSON
                      [{work_order, mpr_month, employees[]}]  ← the main endpoint.
GET  /docs            Auto-generated OpenAPI / Swagger UI.

Run (Docker is the supported path — see README)
-----------------------------------------------
    docker compose up -d --build

NOTE: keep --workers 1. Surya's model loads into RAM once per worker; extra
workers would multiply that footprint without speeding up our serial workflow.

See ARCHITECTURE.md for the full request flow and how each module fits together.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from extractor import SuryaExtractor
from mpr_grouper import group_mpr

# Surya OCR is single-threaded and CPU-bound (minutes per page on CPU). We serve
# ONE extraction at a time: a second concurrent request gets a clear 503 instead
# of silently queueing behind a multi-minute job (which made the box look hung).
_BUSY = asyncio.Lock()
_BUSY_MESSAGE = (
    "Server is busy processing another document (it handles one at a time on "
    "CPU). Please try again in a few minutes."
)


# ---------------------------------------------------------------------------
# Lifespan: load Surya models once at startup; tear down on shutdown
# ---------------------------------------------------------------------------

_extractor: SuryaExtractor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _extractor
    _extractor = SuryaExtractor()
    yield
    _extractor = None


app = FastAPI(
    title="Surya 2 MPR Extractor API",
    description=(
        "POST a NICSI MPR PDF to /extract-grouped to receive the grouped "
        "employee JSON [{work_order, mpr_month, employees[]}]. Built on Surya 2."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index() -> dict[str, Any]:
    """API root — points callers at the endpoints (no web UI; API only)."""
    return {
        "service": "surya2-mpr-extractor",
        "endpoints": {
            "POST /extract-grouped": "PDF → [{work_order, mpr_month, employees[]}]",
            "POST /extract": "PDF → raw per-page blocks (label + html)",
            "GET /health": "liveness + model-loaded status",
            "GET /docs": "OpenAPI / Swagger UI",
        },
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model_loaded": _extractor is not None,
    }


@app.post("/extract")
async def extract(
    file: UploadFile = File(..., description="The PDF file to extract."),
    dpi: int = 200,
) -> JSONResponse:
    """Extract tables from an uploaded PDF.

    Saves the upload to a temporary file, runs Surya, returns JSON.
    """
    if _extractor is None:
        raise HTTPException(503, "Surya models are still loading.")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file.")

    if _BUSY.locked():
        raise HTTPException(503, _BUSY_MESSAGE, headers={"Retry-After": "120"})

    async with _BUSY:
        # Write upload to a temp file (pdf2image needs a path, not bytes)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        try:
            # Run the blocking, CPU-bound OCR in a worker thread so the event
            # loop stays free to answer /health and reject concurrent requests.
            result = await asyncio.to_thread(_extractor.extract_from_pdf, tmp_path, dpi=dpi)
            result["file"] = file.filename  # preserve original name in response
        except Exception as e:
            raise HTTPException(500, f"Extraction failed: {e!r}")
        finally:
            tmp_path.unlink(missing_ok=True)

    return JSONResponse(result)


@app.post("/extract-grouped")
async def extract_grouped(
    file: UploadFile = File(..., description="The MPR PDF file to extract."),
    dpi: int = 300,
) -> JSONResponse:
    """Extract a NICSI MPR PDF and return the grouped per-month JSON shape:

        [
          {
            "work_order": "M2602757",
            "mpr_month": "February 2026",
            "employees": [
              {"employee_name": "...", "designation": "...", "leaves": 0},
              ...
            ]
          },
          ...
        ]

    This is the high-level endpoint most callers want — it runs Surya OCR and
    reshapes the result into one record per (work order, month).
    """
    if _extractor is None:
        raise HTTPException(503, "Surya models are still loading.")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file.")

    if _BUSY.locked():
        raise HTTPException(503, _BUSY_MESSAGE, headers={"Retry-After": "120"})

    async with _BUSY:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(await file.read())
            tmp_path = Path(tmp.name)

        try:
            # OCR is the slow, blocking part — offload it to a worker thread so
            # the event loop keeps answering /health and rejecting concurrent
            # uploads with 503. group_mpr is fast pure-Python.
            raw = await asyncio.to_thread(_extractor.extract_from_pdf, tmp_path, dpi=dpi)
            grouped = group_mpr(raw)
        except Exception as e:
            raise HTTPException(500, f"Extraction failed: {e!r}")
        finally:
            tmp_path.unlink(missing_ok=True)

    return JSONResponse(grouped)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """`uv run surya-server` entry point — starts uvicorn."""
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        workers=1,           # see NOTE in module docstring
        reload=False,
    )


if __name__ == "__main__":
    run()
