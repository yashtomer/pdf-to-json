"""NVIDIA NIM backends for MPR (vision), Work Order, and Payment Advice.

Mirrors the Groq paths (groq.py) but runs the model on **NVIDIA NIM** via
langchain-nvidia-ai-endpoints (ChatNVIDIA), against the hosted OpenAI-compatible
endpoint at integrate.api.nvidia.com. The default model is a vision-capable Llama 4
(Scout), so the same MPR image pipeline works; Work Order and Payment Advice reuse
the shared text-first pipelines (with the image fallback for scans).

ChatNVIDIA accepts the standard {"type": "image_url", "image_url": {"url":
"data:image/jpeg;base64,..."}} content block — exactly what `_pdf_to_image_blocks`
emits — so no NVIDIA-specific image handling is needed.

(The Form 11 NVIDIA backend lives in form11.py, like the Groq one.)
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_nvidia_ai_endpoints import ChatNVIDIA

from .config import settings
from .extractor import SYSTEM_PROMPT as MPR_PROMPT
from .extractor import _merge_by_work_order_month, _pdf_to_image_blocks
from .mpr_reconcile import reconcile_multimonth_leaves
from .payment_advice import SYSTEM_PROMPT as PA_PROMPT
from .payment_advice import run_payment_advice
from .schemas import MPRDocument, MPRRecord, PaymentAdvice, WorkOrder
from .workorder import SYSTEM_PROMPT as WO_PROMPT
from .workorder import _pdf_text, run_workorder


def _structured(schema):
    """A ChatNVIDIA client bound to `schema` for structured output (shared by all
    three backends). Raises if the key isn't configured. If a NIM model rejects the
    default tool-calling structured output, set a different NVIDIA_MODEL that supports
    tools (e.g. a Llama 3.1/4 instruct model)."""
    if not settings.nvidia_api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set. Add it to the .env file and restart.")
    llm = ChatNVIDIA(
        model=settings.nvidia_model,
        api_key=settings.nvidia_api_key,
        temperature=0,
    )
    return llm.with_structured_output(schema)


def extract_grouped_nvidia(pdf_path: Path) -> list[MPRRecord]:
    """MPR PDF -> grouped records, read from the page images by NVIDIA NIM (vision).

    For digital PDFs the extracted text layer is passed alongside the images so the
    model reads names/dates/Leave Adjustment Certificates accurately (as on the Groq
    path); the deterministic multi-month reconciler then fixes the per-month split."""
    image_blocks = _pdf_to_image_blocks(pdf_path)
    if not image_blocks:
        return []
    prompt = "Extract the MPR data from these page images."
    text = _pdf_text(pdf_path)
    if len(text.strip()) >= 50:
        prompt += (
            " The document's extracted text is provided below — use it to read names, "
            "dates and Leave Adjustment Certificates accurately:\n\n" + text
        )
    content = [{"type": "text", "text": prompt}, *image_blocks]
    result: MPRDocument = _structured(MPRDocument).invoke(
        [SystemMessage(content=MPR_PROMPT), HumanMessage(content=content)]
    )
    records = _merge_by_work_order_month(result.records)
    return reconcile_multimonth_leaves(text, records)


def extract_workorder_nvidia(pdf_path: Path) -> WorkOrder:
    """Work Order PDF -> structured fields via NVIDIA NIM. Text for digital PDFs,
    page images (+ majority vote) for scans — the shared pipeline + reconciliation."""
    def invoke(content):
        return _structured(WorkOrder).invoke(
            [SystemMessage(content=WO_PROMPT), HumanMessage(content=content)]
        )
    return run_workorder(pdf_path, invoke)


def extract_payment_advice_nvidia(pdf_path: Path) -> PaymentAdvice:
    """Payment Advice PDF -> structured fields via NVIDIA NIM (text path + image fallback)."""
    def invoke(content):
        return _structured(PaymentAdvice).invoke(
            [SystemMessage(content=PA_PROMPT), HumanMessage(content=content)]
        )
    return run_payment_advice(pdf_path, invoke)
