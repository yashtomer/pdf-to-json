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
from .schemas import Form11, MPRRecord, PaymentAdvice, WorkOrder
from .workorder import extract_workorder
from .payment_advice import extract_payment_advice
from .form11 import extract_form11, extract_form11_groq
from .workorder_local import extract_workorder_local
from .mpr_local import extract_grouped_vision
from .gemini import extract_grouped_gemini, extract_workorder_gemini
from .groq import (
    extract_grouped_groq,
    extract_payment_advice_groq,
    extract_workorder_groq,
)

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


# Accept PDFs and common image formats (MPRs are often phone photos / scans).
_ALLOWED_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif")


def _validate_upload(file: UploadFile) -> None:
    name = (file.filename or "").lower()
    if not name.endswith(_ALLOWED_SUFFIXES):
        raise HTTPException(400, "Please upload a PDF or image file (pdf/jpg/png/…).")

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
        "gemini_configured": bool(settings.google_api_key),
        "auth_enabled": bool(settings.auth_key_set),
    }


@app.post(
    "/extract-grouped",
    response_model=list[MPRRecord],
    tags=["extraction"],
    summary=f"MPR PDF -> grouped JSON  ·  Claude ({settings.anthropic_model})",
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
    _validate_upload(file)

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
    summary=f"Work Order PDF -> structured JSON  ·  Claude ({settings.anthropic_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_workorder_endpoint(
    file: UploadFile = File(..., description="The NICSI Work Order PDF."),
) -> WorkOrder:
    """Parse a NICSI Work Order into structured fields + line items. Auto-detects
    `tender_type` (`tier_3` vs `support_engineer`)."""
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)

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
    "/extract-payment-advice",
    response_model=PaymentAdvice,
    tags=["extraction"],
    summary=f"Payment Advice PDF -> JSON  ·  Claude ({settings.anthropic_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_payment_advice_endpoint(
    file: UploadFile = File(..., description="The NICSI Payment Advice (RTGS/NEFT transfer) PDF."),
) -> PaymentAdvice:
    """Parse a NICSI Payment Advice into the net amount paid (`pa_amount`), the advice
    date (`pa_date`), and the enclosed `bills` — each mapping a `bill_no` to its
    `work_order` (the PO. No.)."""
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_payment_advice, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-form11",
    response_model=Form11,
    tags=["extraction"],
    summary=f"EPF Form 11 (Declaration) -> JSON  ·  Claude ({settings.anthropic_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_form11_endpoint(
    file: UploadFile = File(..., description="The EPF Form 11 (Declaration Form) PDF or image."),
) -> Form11:
    """Parse an EPFO Form 11 (Declaration Form) into the member's identity + KYC
    fields: `employee_name`, `uan_no`, `aadhar_no`, `email`, `phone`, `account_no`,
    `ifsc` and `pan_no`. The form is a hand-filled scan, so Claude reads the image."""
    if not settings.anthropic_api_key:
        raise HTTPException(503, "ANTHROPIC_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_form11, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-form11-groq",
    response_model=Form11,
    tags=["extraction"],
    summary=f"EPF Form 11 (Declaration) -> JSON  ·  Groq ({settings.groq_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_form11_groq_endpoint(
    file: UploadFile = File(..., description="The EPF Form 11 (Declaration Form) PDF or image."),
) -> Form11:
    """Same Form 11 output as /extract-form11, read by a vision-capable Llama 4 on
    Groq instead of Claude (fast + a generous free tier)."""
    if not settings.groq_api_key:
        raise HTTPException(503, "GROQ_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_form11_groq, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-workorder-with-local-llm",
    tags=["extraction"],
    summary=f"Work Order PDF -> JSON  ·  local Ollama ({settings.ollama_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_workorder_local_endpoint(
    file: UploadFile = File(..., description="The NICSI Work Order PDF."),
) -> dict:
    """Same as /extract-workorder but the model runs on this server via Ollama
    (free + private). On a CPU host this is SLOW (minutes/doc). The response
    includes the `model` used and `seconds` taken so you can benchmark it.
    """
    _validate_upload(file)

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


@app.post(
    "/extract-grouped-qwen3-vl",
    tags=["extraction"],
    summary=f"MPR PDF -> grouped JSON  ·  local vision Ollama ({settings.ollama_vision_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_grouped_qwen3vl_endpoint(
    file: UploadFile = File(..., description="The MPR PDF (scanned ok)."),
) -> dict:
    """Same grouped MPR output as /extract-grouped, but read by a LOCAL vision
    model (Ollama, default qwen3-vl:8b) instead of Claude — free + private. The
    response includes `model` and `seconds` for benchmarking."""
    _validate_upload(file)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_grouped_vision, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Local vision extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-grouped-gemini",
    response_model=list[MPRRecord],
    tags=["extraction"],
    summary=f"MPR PDF -> grouped JSON  ·  Google Gemini ({settings.gemini_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_grouped_gemini_endpoint(
    file: UploadFile = File(..., description="The MPR PDF (scanned ok)."),
) -> list[MPRRecord]:
    """Same grouped MPR output as /extract-grouped, read by Google Gemini Flash."""
    if not settings.google_api_key:
        raise HTTPException(503, "GOOGLE_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_grouped_gemini, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Gemini extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-workorder-gemini",
    response_model=WorkOrder,
    tags=["extraction"],
    summary=f"Work Order PDF -> JSON  ·  Google Gemini ({settings.gemini_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_workorder_gemini_endpoint(
    file: UploadFile = File(..., description="The NICSI Work Order PDF."),
) -> WorkOrder:
    """Same work-order output as /extract-workorder, via Google Gemini."""
    if not settings.google_api_key:
        raise HTTPException(503, "GOOGLE_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_workorder_gemini, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Gemini extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-grouped-groq",
    response_model=list[MPRRecord],
    tags=["extraction"],
    summary=f"MPR PDF -> grouped JSON  ·  Groq ({settings.groq_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_grouped_groq_endpoint(
    file: UploadFile = File(..., description="The MPR PDF (scanned ok)."),
) -> list[MPRRecord]:
    """Same grouped MPR output as /extract-grouped, read by a vision-capable Llama 4
    on Groq instead of Claude (fast + a generous free tier)."""
    if not settings.groq_api_key:
        raise HTTPException(503, "GROQ_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_grouped_groq, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Groq extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-workorder-groq",
    response_model=WorkOrder,
    tags=["extraction"],
    summary=f"Work Order PDF -> JSON  ·  Groq ({settings.groq_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_workorder_groq_endpoint(
    file: UploadFile = File(..., description="The NICSI Work Order PDF."),
) -> WorkOrder:
    """Same work-order output as /extract-workorder, via Groq (Llama 4)."""
    if not settings.groq_api_key:
        raise HTTPException(503, "GROQ_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_workorder_groq, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Groq extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post(
    "/extract-payment-advice-groq",
    response_model=PaymentAdvice,
    tags=["extraction"],
    summary=f"Payment Advice PDF -> JSON  ·  Groq ({settings.groq_model})",
    dependencies=[Depends(require_api_key)],
)
async def extract_payment_advice_groq_endpoint(
    file: UploadFile = File(..., description="The NICSI Payment Advice (RTGS/NEFT transfer) PDF."),
) -> PaymentAdvice:
    """Same Payment Advice output as /extract-payment-advice, via Groq (Llama 4)."""
    if not settings.groq_api_key:
        raise HTTPException(503, "GROQ_API_KEY not set — add it to .env and restart.")
    _validate_upload(file)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return await asyncio.to_thread(extract_payment_advice_groq, tmp_path)
    except Exception as e:
        raise HTTPException(500, f"Groq extraction failed: {e!r}")
    finally:
        tmp_path.unlink(missing_ok=True)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    run()
