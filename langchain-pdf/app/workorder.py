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


def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


_INC_RE = re.compile(r"with\s+([\w]+)\s+increment", re.IGNORECASE)

# Each work-order category has a distinct, unambiguous signature in the line-item
# descriptions / HSN code, so we can classify deterministically and not rely on the
# model getting `tender_type` right (especially on scanned docs read from images).
_GIS_RE = re.compile(r"gis\s+digitization", re.IGNORECASE)
_TIER3_RE = re.compile(r"tier\s*-?\s*3", re.IGNORECASE)
_SE_RE = re.compile(r"software\s+application\s+support\s+engineer", re.IGNORECASE)


def _classify_tender_type(wo: WorkOrder) -> str | None:
    """Infer tender_type from the line items. Returns None if nothing matches (then
    we keep whatever the model produced)."""
    descs = " ".join((it.description or "") for it in wo.items)
    hsns = {(it.hsn_code or "").strip() for it in wo.items}
    if _GIS_RE.search(descs) or "998319" in hsns:
        return "gis"
    if _TIER3_RE.search(descs) or "998314" in hsns or "(tier-3)" in (wo.tender_number or "").lower():
        return "tier_3"
    if _SE_RE.search(descs) or "998313" in hsns:
        return "support_engineer"
    return None


def _increment(desc: str) -> str:
    m = _INC_RE.search(desc or "")
    return m.group(1).lower() if m else ""


def _fix_levels_by_rate(items) -> None:
    """Within a work order, unit_rate increases monotonically with the level (same
    increment). The rate is read reliably; the level digit on a blurry scan is not.
    So if a row's level is inconsistent with where its rate sits among the others
    AND the bound is unique, correct the level (and the 'Level N' in the description).
    Skipped unless all rows share the same increment (keeps the monotonic assumption
    valid)."""
    rated = [it for it in items if it.designation_level is not None and it.unit_rate]
    if len(rated) < 3 or len({_increment(it.description) for it in rated}) > 1:
        return
    for it in rated:
        lower = [o.designation_level for o in rated if o.unit_rate < it.unit_rate]
        upper = [o.designation_level for o in rated if o.unit_rate > it.unit_rate]
        lo = (max(lower) + 1) if lower else None
        hi = (min(upper) - 1) if upper else None
        cur = it.designation_level
        out_of_range = (lo is not None and cur < lo) or (hi is not None and cur > hi)
        if out_of_range and lo is not None and hi is not None and lo == hi:
            it.designation_level = lo
            it.description = re.sub(r"Level\s+\d+", f"Level {lo}", it.description or "", count=1)


def reconcile_workorder(wo: WorkOrder) -> WorkOrder:
    """Deterministic corrections so model mis-reads can't slip through.

    1. designation_level = the N in 'Level N' in the description (None if no Level).
    2. Arithmetic: each line_total = manpower × period × unit_rate. unit_rate (a
       small column) is the most mis-read field. When line_totals are trustworthy
       — they sum to the printed grand total (taxable_amount) — and a row's
       arithmetic doesn't hold against the period the other rows agree on, recompute
       that row's unit_rate from its line_total. Keep item.taxable_amount == line_total.
    """
    for it in wo.items:
        m = _LEVEL_RE.search(it.description or "")
        it.designation_level = int(m.group(1)) if m else None

    # Deterministically set the category from the line-item signatures (overrides a
    # model misread). gis/support_engineer rows have no 'Level', so their
    # designation_level is already None from the loop above.
    t = _classify_tender_type(wo)
    if t:
        wo.tender_type = t

    _fix_levels_by_rate(wo.items)

    import statistics

    items = wo.items

    def implied(it):
        if it.manpower_count and it.unit_rate and it.line_total:
            return it.line_total / (it.manpower_count * it.unit_rate)
        return None

    def off(it, P):  # rupee discrepancy of a row against period P
        return abs(it.manpower_count * P * it.unit_rate - it.line_total)

    valid = [p for p in (implied(it) for it in items) if p]
    if len(valid) >= 2:
        P0 = statistics.median(valid)
        consistent = [it for it in items
                      if it.manpower_count and it.unit_rate and off(it, P0) <= 1.0]
        grand = _num(wo.taxable_amount)
        line_sum = sum(it.line_total for it in items)
        trustworthy = grand is None or abs(line_sum - grand) <= max(2.0, 0.005 * grand)
        # Only act when a strict MAJORITY of rows agree on the period (so single-item
        # or fractional-period work orders are never "corrected" against noise).
        if trustworthy and len(consistent) > len(valid) / 2:
            P = statistics.median([implied(it) for it in consistent])
            for it in items:
                if it.manpower_count and it.unit_rate and off(it, P) > 1.0:
                    it.unit_rate = round(it.line_total / (it.manpower_count * P), 2)

    for it in items:
        it.taxable_amount = it.line_total

    # Top-level taxable_amount is, by definition, the sum of the line totals
    # ("Total Amount in Rs."). Derive it from the (reconciled) items so a model
    # mis-read of that figure can't slip through. NICSI rounds to the rupee.
    if items:
        wo.taxable_amount = str(round(sum(it.line_total for it in items)))
    return wo


# kept for backwards-compat import name
fix_designation_levels = reconcile_workorder

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
- 'gis' — line items read 'GIS Digitization …' (e.g. 'GIS Digitization Supervisor');
  HSN/SAC is 998319; the Empanelment No has no '(Tier-3)'. Set designation_level
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


def _mode(values):
    """Most common non-empty value; falls back to the first value."""
    from collections import Counter
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return values[0] if values else None
    return Counter(vals).most_common(1)[0][0]


_WO_FIELDS = ("work_order_number", "project_number", "project_name", "date_issued",
              "wo_total_value", "tender_number", "valid_till_date", "pi_number",
              "user_contact_detail", "doc_type", "tender_type", "taxable_amount")
_ITEM_FIELDS = ("line_no", "hsn_code", "description", "designation_level",
                "manpower_count", "period_text", "start_date", "end_date",
                "unit_rate", "taxable_amount", "line_total")


def _vote_workorders(wos: list[WorkOrder]) -> WorkOrder:
    """Per-field majority vote across repeated extractions of the same doc — smooths
    out vision-OCR variance (e.g. a degraded 'Level 3' read as '5' in 1 of 3 runs)."""
    base = wos[0]
    for f in _WO_FIELDS:
        setattr(base, f, _mode([getattr(w, f) for w in wos]))
    n_items = _mode([len(w.items) for w in wos]) or len(base.items)
    voted = []
    for i in range(n_items):
        rows = [w.items[i] for w in wos if i < len(w.items)]
        it = rows[0]
        for f in _ITEM_FIELDS:
            setattr(it, f, _mode([getattr(r, f) for r in rows]))
        voted.append(it)
    base.items = voted
    return base


def run_workorder(pdf_path: Path, invoke) -> WorkOrder:
    """Shared work-order pipeline. `invoke(content) -> WorkOrder` is the model call.
    Digital PDFs: one text pass. Scanned PDFs (image fallback): an N-run majority
    vote to absorb OCR variance. Then deterministic reconciliation."""
    text = _pdf_text(pdf_path)
    if len(text.strip()) >= 200:
        content: list = [
            {"type": "text", "text": "Extract the work order. Here is its text:\n\n" + text}
        ]
        wo = invoke(content)
    else:
        content = [
            {"type": "text", "text": "Extract the work order from these page images."},
            *_pdf_to_image_blocks(pdf_path),
        ]
        runs = max(1, settings.workorder_scan_runs)
        wos = [invoke(content) for _ in range(runs)]
        wo = _vote_workorders(wos) if len(wos) > 1 else wos[0]
    return reconcile_workorder(wo)


def _invoke_claude(content):
    structured = _llm().with_structured_output(WorkOrder)
    return structured.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=content)])


def extract_workorder(pdf_path: Path) -> WorkOrder:
    return run_workorder(pdf_path, _invoke_claude)
