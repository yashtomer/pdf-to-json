"""Groq backends for MPR (vision), Work Order (text/image) and Payment Advice.

Mirrors the Claude paths (extractor.py / workorder.py / payment_advice.py) and the
Gemini paths (gemini.py), but runs the model on Groq via langchain-groq. The default
model is a vision-capable Llama 4 (Scout), so the same MPR image pipeline works;
Work Order and Payment Advice reuse the shared text-first pipelines (with the image
fallback for scans). Groq is fast and has a generous free tier.

(The Form 11 Groq backend lives in form11.py instead — it needs Form-11-specific
output normalization, so it shares that module's `run_form11`.)
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from .config import settings
from .extractor import SYSTEM_PROMPT as MPR_PROMPT
from .extractor import _merge_by_work_order_month, _pdf_to_image_blocks
from .mpr_reconcile import reconcile_multimonth_leaves
from .payment_advice import SYSTEM_PROMPT as PA_PROMPT
from .payment_advice import run_payment_advice
from .schemas import MPRRecord, MPRDocument, PaymentAdvice, WorkOrder
from .workorder import SYSTEM_PROMPT as WO_PROMPT
from .workorder import _pdf_text, run_workorder


def _structured(schema):
    """A ChatGroq client bound to `schema` for structured output (shared by all
    three backends). Raises if the key isn't configured.

    Uses Groq's `json_schema` structured-output mode (constrained decoding) rather
    than the default tool-calling. Groq tool-calling validates the model's JSON
    against the schema STRICTLY and 400s on a type mismatch (e.g. Llama returning
    an int field as a string), whereas json_schema forces the model to emit the
    right types — so numeric fields (pa_amount, leaves, unit_rate…) come back clean.
    """
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to the .env file and restart.")
    llm = ChatGroq(model=settings.groq_model, api_key=settings.groq_api_key, temperature=0)
    return llm.with_structured_output(schema, method="json_schema")


def extract_grouped_groq(pdf_path: Path) -> list[MPRRecord]:
    """MPR PDF -> grouped records, read from the page images by Groq (vision).

    For DIGITAL PDFs we also pass the extracted text layer alongside the images.
    Llama 4 Scout reads dates off a scanned image unreliably and tends to dump the
    summary 'Absent' total into the first month; given the certificate TEXT it can
    split a multi-month MPR's leaves into the correct month. Scanned MPRs (phone
    photos) have no text layer, so this is a no-op for them — images only, as before.
    """
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
    # Deterministically fix the per-month split for multi-month MPRs: Llama reads
    # the certificate dates but mis-buckets them. No-op when there's no text layer
    # (scanned photos) or the certificate total disagrees with the model.
    return reconcile_multimonth_leaves(text, records)


def extract_workorder_groq(pdf_path: Path) -> WorkOrder:
    """Work Order PDF -> structured fields via Groq. Text for digital PDFs, page
    images (+ majority vote) for scans — the shared pipeline + deterministic
    reconciliation, just with the Groq model."""
    def invoke(content):
        return _structured(WorkOrder).invoke(
            [SystemMessage(content=WO_PROMPT), HumanMessage(content=content)]
        )
    return run_workorder(pdf_path, invoke)


def extract_payment_advice_groq(pdf_path: Path) -> PaymentAdvice:
    """Payment Advice PDF -> structured fields via Groq (text path + image fallback)."""
    def invoke(content):
        return _structured(PaymentAdvice).invoke(
            [SystemMessage(content=PA_PROMPT), HumanMessage(content=content)]
        )
    return run_payment_advice(pdf_path, invoke)
