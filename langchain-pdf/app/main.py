"""FastAPI app: POST an MPR PDF, get grouped JSON extracted by Claude.

Swagger UI is served automatically at /docs (and ReDoc at /redoc); the raw
OpenAPI spec is at /openapi.json.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from .config import settings
from .extractor import extract_grouped
from .schemas import MPRRecord

app = FastAPI(
    title="LangChain MPR Extractor (Claude)",
    description=(
        "Upload a NICSI MPR PDF to **/extract-grouped** and receive the grouped "
        "employee JSON `[{work_order, mpr_month, employees[]}]`.\n\n"
        "Reading + structuring is done by Anthropic Claude via LangChain — no OCR "
        "engine or GPU needed. Configure the model and key in `.env`."
    ),
    version="1.0.0",
    contact={"name": "pdf-to-json"},
)


@app.get("/health", tags=["meta"], summary="Liveness + config status")
def health() -> dict:
    return {
        "status": "ok",
        "model": settings.anthropic_model,
        "api_key_configured": bool(settings.anthropic_api_key),
    }


@app.post(
    "/extract-grouped",
    response_model=list[MPRRecord],
    tags=["extraction"],
    summary="MPR PDF -> grouped JSON",
)
async def extract_grouped_endpoint(
    file: UploadFile = File(..., description="The MPR PDF file to extract."),
) -> list[MPRRecord]:
    """Render the PDF pages and have Claude return one record per
    `(work_order, mpr_month)` with each employee's name, designation and leaves.
    """
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not set — add it to .env and restart.")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        # extract_grouped is blocking (pdf render + Anthropic call) — run it in a
        # worker thread so the event loop stays free and many uploads can be in
        # flight at once (the Anthropic call is I/O-bound, so this parallelizes).
        return await asyncio.to_thread(extract_grouped, tmp_path)
    except Exception as e:  # surface a clean error to the caller
        raise HTTPException(500, f"Extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    run()
