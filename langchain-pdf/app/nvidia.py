"""NVIDIA NIM backends for MPR, Work Order and Payment Advice (Form 11 → form11.py).

Runs the model on **NVIDIA NIM** via langchain-nvidia-ai-endpoints (ChatNVIDIA),
against the hosted endpoint at integrate.api.nvidia.com.

NIM's vision models (e.g. Llama 4 Maverick) are NOT reliable with LangChain's
`with_structured_output` (schema-enforced tool-calling) — fields silently drop. So,
exactly like the local Ollama paths (mpr_local.py / workorder_local.py), we prompt the
model for JSON of the target shape and parse it ourselves. ChatNVIDIA accepts the
standard {"type": "image_url", "image_url": {"url": "data:..."}} content block, so the
shared image pipeline + prompts + reconciliation layers are all reused unchanged.
"""

from __future__ import annotations

import json
import re
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

# Compact JSON shapes appended to each prompt so the model emits the exact structure.
_MPR_SHAPE = (
    'Return ONLY a JSON object (no markdown fences, no prose) of exactly this shape: '
    '{"records":[{"work_order":"string","mpr_month":"Month YYYY","signature_date":'
    '"date as printed or empty string","ai_score":0,"employees":[{"employee_name":'
    '"string","designation":"string","leaves":0}]}]}'
)
_WO_SHAPE = (
    'Return ONLY a JSON object (no markdown fences, no prose) of exactly this shape: '
    '{"work_order_number":"","project_number":"","project_name":"","date_issued":"",'
    '"wo_total_value":"","tender_number":"","valid_till_date":"","pi_number":"",'
    '"user_contact_detail":"","doc_type":"work_order","tender_type":"","taxable_amount":"",'
    '"ai_score":0,"items":[{"line_no":1,"hsn_code":"","description":"","designation_level":'
    'null,"manpower_count":0,"period_text":"","start_date":"","end_date":"","unit_rate":0,'
    '"taxable_amount":0,"line_total":0}]}'
)
_PA_SHAPE = (
    'Return ONLY a JSON object (no markdown fences, no prose) of exactly this shape: '
    '{"pa_amount":0,"pa_date":"","ai_score":0,"bills":[{"bill_no":"","work_order":""}]}'
)


def _llm() -> ChatNVIDIA:
    if not settings.nvidia_api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set. Add it to the .env file and restart.")
    return ChatNVIDIA(
        model=settings.nvidia_model,
        api_key=settings.nvidia_api_key,
        temperature=0,
        max_tokens=4096,
        timeout=settings.nvidia_timeout,
    )


def _parse_json(text: str):
    """Pull the JSON value out of the model's reply (tolerates fences / prose and a
    leading object or array)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    ends = [i for i in (t.rfind("}"), t.rfind("]")) if i != -1]
    if starts and ends:
        t = t[min(starts):max(ends) + 1]
    return json.loads(t)


def nvidia_json_invoke(system_prompt: str, content, shape_hint: str) -> dict:
    """Ask NVIDIA NIM for JSON of `shape_hint` and parse it into a dict. Shared with
    the Form 11 NVIDIA path. `content` is a str or a list of content blocks."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    msg = [*content, {"type": "text", "text": shape_hint}]
    resp = _llm().invoke([SystemMessage(content=system_prompt), HumanMessage(content=msg)])
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    return _parse_json(text)


def extract_grouped_nvidia(pdf_path: Path) -> list[MPRRecord]:
    """MPR PDF -> grouped records via NVIDIA NIM (vision + text layer + reconcile)."""
    image_blocks = _pdf_to_image_blocks(pdf_path)
    if not image_blocks:
        return []
    text = _pdf_text(pdf_path)
    prompt = "Extract the MPR data from these page images."
    if len(text.strip()) >= 50:
        prompt += (
            " The document's extracted text is provided below — use it to read names, "
            "dates and Leave Adjustment Certificates accurately:\n\n" + text
        )
    content = [{"type": "text", "text": prompt}, *image_blocks]
    data = nvidia_json_invoke(MPR_PROMPT, content, _MPR_SHAPE)
    if isinstance(data, list):          # model returned a bare records array
        data = {"records": data}
    records = _merge_by_work_order_month(MPRDocument(**data).records)
    return reconcile_multimonth_leaves(text, records)


def extract_workorder_nvidia(pdf_path: Path) -> WorkOrder:
    """Work Order PDF -> structured fields via NVIDIA NIM (shared pipeline + reconcile)."""
    def invoke(content):
        return WorkOrder(**nvidia_json_invoke(WO_PROMPT, content, _WO_SHAPE))
    return run_workorder(pdf_path, invoke)


def extract_payment_advice_nvidia(pdf_path: Path) -> PaymentAdvice:
    """Payment Advice PDF -> structured fields via NVIDIA NIM (text + image fallback)."""
    def invoke(content):
        return PaymentAdvice(**nvidia_json_invoke(PA_PROMPT, content, _PA_SHAPE))
    return run_payment_advice(pdf_path, invoke)
