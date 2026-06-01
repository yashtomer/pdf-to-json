"""Work Order PDF → structured JSON, using Claude on the extracted TEXT.

NICSI work orders are digital (text) PDFs, so we extract layout text with
`pdftotext -layout` (poppler) and send that to Claude — cheaper and more accurate
than images. If a work order is ever a scan (little/no extractable text) we fall
back to page images.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from .config import settings
from .extractor import _pdf_to_image_blocks  # reuse the image renderer for the fallback
from .schemas import WorkOrder

_LEVEL_RE = re.compile(r"\bLevel\s+(\d+)", re.IGNORECASE)


def fix_designation_levels(wo: WorkOrder) -> WorkOrder:
    """designation_level is, by definition, the N in 'Level N' in the description.
    Derive it deterministically so a model mis-read (e.g. 'Level 3' -> 5) can't
    slip through. No 'Level' (e.g. Support Engineer rows) -> None."""
    for it in wo.items:
        m = _LEVEL_RE.search(it.description or "")
        it.designation_level = int(m.group(1)) if m else None
    return wo

SYSTEM_PROMPT = """\
You read NICSI **Work Order** PDFs and return the structured fields. You are given
the extracted text of one work order (and, as a fallback, its page images).

Identify the work-order TYPE and set `tender_type`:
- 'tier_3'  — line items read 'Level N (…) - Tier 3'; HSN/SAC is 998314; the
  Empanelment No contains '(Tier-3)'. For each such item set designation_level to
  the number N from 'Level N'.
- 'support_engineer' — line items read 'Software Application Support Engineer (…)';
  HSN/SAC is 998313; the Empanelment No has no '(Tier-3)'. Set designation_level
  to null for these items.

Field sources:
- work_order_number = 'Work Order No'; project_number = 'Project No';
  project_name = 'Project Name'; date_issued = the 'Date'.
- tender_number = the 'Empanelment No'; valid_till_date = the 'Valid Till:' date.
- pi_number = 'PI Number' (often blank → empty string).
- user_contact_detail = ONLY the Project Manager's NAME (the text BEFORE the first
  comma) in 'the concerned Project Manager (<name>, <title>) at NICSI…'. Example:
  from '(Neeraj Chawla, Deputy General Manager)' output exactly 'Neeraj Chawla' —
  name only, no title, no comma. Do NOT use the 'Issued to' agency contact person.
- wo_total_value = 'Grand Total (in Rs.)' — DIGITS ONLY (strip commas/decimals as
  printed; it is a whole-rupee figure).
- taxable_amount (top level) = 'Total Amount in Rs.' — DIGITS ONLY.

Line item columns (the order table): line_no = S.No; hsn_code = HSN/SAC Code;
description = full Description; manpower_count = 'No of Persons Required' (A);
period_text = 'Required Period' (B); unit_rate = 'Unit Rate per Month' excluding
taxes (C); taxable_amount and line_total = 'Total Amount' (E = A×B×C); start_date
and end_date = the From/To of 'Date of Deployment' (D). Strip thousands commas
from all numbers (Indian grouping like 3,40,005.60 → 340005.60). Read values
exactly as printed; never invent. doc_type is always 'work_order'.
"""


def _pdf_text(pdf_path: Path) -> str:
    """Extract layout-preserving text with poppler's pdftotext."""
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout or ""
    except Exception:
        return ""


def _llm() -> ChatAnthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to the .env file and restart.")
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        max_tokens=settings.anthropic_max_tokens,
        timeout=settings.anthropic_timeout,
        temperature=settings.anthropic_temperature,
    )


def extract_workorder(pdf_path: Path) -> WorkOrder:
    text = _pdf_text(pdf_path)
    if len(text.strip()) >= 200:
        content: list = [
            {"type": "text", "text": "Extract the work order. Here is its text:\n\n" + text}
        ]
    else:
        # scanned / no text → fall back to page images
        content = [
            {"type": "text", "text": "Extract the work order from these page images."},
            *_pdf_to_image_blocks(pdf_path),
        ]
    structured = _llm().with_structured_output(WorkOrder)
    wo = structured.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=content)])
    return fix_designation_levels(wo)
