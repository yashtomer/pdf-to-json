"""FastAPI app: POST an MPR PDF, get grouped JSON extracted by Claude.

Swagger UI is served automatically at /docs (and ReDoc at /redoc); the raw
OpenAPI spec is at /openapi.json.
"""

from __future__ import annotations

import asyncio
import hmac
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader

from .config import settings
from .extractor import extract_grouped
from .schemas import MPRRecord, WorkOrder
from .workorder import extract_workorder
from .workorder_local import extract_workorder_local

# API-key auth on the extraction endpoint. Callers send `X-API-Key: <key>`.
# Keys come from API_AUTH_KEYS in .env (comma-separated). If none are configured,
# auth is disabled (open) so the service still works until keys are set.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(provided: str = Security(_api_key_header)) -> None:
    allowed = settings.auth_key_set
    if not allowed:
        return  # auth disabled — no keys configured
    if provided and any(hmac.compare_digest(provided, k) for k in allowed):
        return  # valid key
    raise HTTPException(401, "Missing or invalid API key (send X-API-Key header).")

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
        "auth_enabled": bool(settings.auth_key_set),
    }


@app.post(
    "/extract-grouped",
    response_model=list[MPRRecord],
    tags=["extraction"],
    summary="MPR PDF -> grouped JSON",
    dependencies=[Depends(require_api_key)],
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


@app.post(
    "/extract-workorder",
    response_model=WorkOrder,
    tags=["extraction"],
    summary="Work Order PDF -> structured JSON",
    dependencies=[Depends(require_api_key)],
)
async def extract_workorder_endpoint(
    file: UploadFile = File(..., description="The NICSI Work Order PDF."),
) -> WorkOrder:
    """Parse a NICSI Work Order into structured fields + line items. Auto-detects
    `tender_type` (`tier_3` vs `support_engineer`)."""
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not set — add it to .env and restart.")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_workorder, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-workorder-with-local-llm",
    tags=["extraction"],
    summary="Work Order PDF -> JSON via a LOCAL LLM (Ollama)",
    dependencies=[Depends(require_api_key)],
)
async def extract_workorder_local_endpoint(
    file: UploadFile = File(..., description="The NICSI Work Order PDF."),
) -> dict:
    """Same as /extract-workorder but the model runs on this server via Ollama
    (free + private). On a CPU host this is SLOW (minutes/doc). The response
    includes the `model` used and `seconds` taken so you can benchmark it.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file.")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        out = await asyncio.to_thread(extract_workorder_local, tmp_path)
        return {"model": out["model"], "seconds": out["seconds"],
                "result": out["result"].model_dump()}
    except Exception as e:
        raise HTTPException(500, f"Local extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    run()
