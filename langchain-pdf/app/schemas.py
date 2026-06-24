"""Pydantic schemas — also drive Claude's structured output.

The Field descriptions are sent to the model (via tool-calling), so they double
as extraction instructions. Keep them precise.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _coerce_ai_score(v: object) -> int:
    """Coerce the model's self-reported confidence to an int percentage in 0-100.
    Tolerates strings ('95'), floats (95.6) and out-of-range / junk values, so a
    stray model output can never break the response (falls back to 0 = needs review)."""
    try:
        n = int(round(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


# A 0-100 confidence percentage the model self-reports. Defined once and reused by
# every schema so all endpoints expose the same `ai_score` field + clamping.
AIScore = Annotated[int, BeforeValidator(_coerce_ai_score)]

_AI_SCORE_DESC = (
    "Your confidence as a percentage from 0 to 100 that this was extracted 100% "
    "correctly from the document. Use 100 only when every value is clearly legible "
    "and certain; lower it for blurry, handwritten or ambiguous scans, or any value "
    "you had to guess. Used downstream to decide which results need a manual check."
)


class Employee(BaseModel):
    employee_name: str = Field(
        description="The person's full name exactly as printed (may be ALL-CAPS). "
        "Empty string if it is genuinely unreadable — never guess."
    )
    designation: str = Field(
        description="The full designation/role text, e.g. "
        "'Level 6 (Minimum work experience 1 years) with 2nd Increment - Tier - 3' "
        "or 'GIS DIGITIZATION SUPERVISOR' or 'Software Application Support Engineer "
        "(0 to less than 2 years relevant experience)'."
    )
    leaves: float = Field(
        description="The number of leaves / total absence for this employee in this "
        "month. A whole number, or a fraction like 0.5 or 1.5 for half-days. "
        "Use the Total Absence / Leaves Taken value — NOT a remark, a date, or an "
        "attendance time. '-' or blank means 0."
    )


class MPRRecord(BaseModel):
    work_order: str = Field(
        description="The work order number, like 'M2602757'. If a single table lists "
        "several work orders (a 'Work Order No.' column), produce one record per "
        "work order with only that work order's employees."
    )
    mpr_month: str = Field(
        description="The report month as 'Month YYYY', e.g. 'April 2026'. If the MPR "
        "covers a RANGE like 'January to March 2026' AND has per-employee Leave "
        "Adjustment Certificates, produce one record per month with that month's "
        "leaves. Empty string if no month is present."
    )
    ai_score: AIScore = Field(default=0, description=_AI_SCORE_DESC)
    employees: list[Employee] = Field(
        description="The employees for this work order and month."
    )


class MPRDocument(BaseModel):
    """Root structured-output container — the model fills `records`."""

    records: list[MPRRecord] = Field(
        description="One record per (work order, month) found in the document."
    )


# ---------------------------------------------------------------------------
# Work Order documents
# ---------------------------------------------------------------------------

class WorkOrderItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line_no: int = Field(default=0, description="The S.No of the line item (1, 2, ...).")
    hsn_code: str = Field(default="", description="The HSN/SAC code, e.g. '998314' (tier_3), '998313' (support_engineer), or '998319' (gis).")
    description: str = Field(default="", description="The full item description text.")
    designation_level: int | None = Field(
        default=None,
        description="For tier_3 rows the number N from 'Level N' (e.g. 7). null for "
        "support_engineer and gis rows (which have no Level).",
    )
    manpower_count: int = Field(default=0, description="No. of Persons Required (column A).")
    period_text: str = Field(default="", description="The Required Period text (column B), e.g. 'Five Month(s)'.")
    start_date: str = Field(default="", description="Deployment start date (the 'From' of column D), as printed (DD/MM/YYYY).")
    end_date: str = Field(default="", description="Deployment end date (the 'To' of column D), as printed.")
    unit_rate: float = Field(default=0, description="Unit Rate per Month excluding taxes (column C), digits only.")
    taxable_amount: float = Field(default=0, description="Line Total Amount excluding taxes (column E = A×B×C).")
    line_total: float = Field(default=0, description="Same as the line's Total Amount (column E).")


class WorkOrder(BaseModel):
    """A NICSI Work Order document → structured fields. Also the tool schema, so the
    Field descriptions instruct the model. Lenient (defaults + ignore extras) so
    that a local-LLM JSON response still validates."""

    # coerce_numbers_to_str: local models often emit wo_total_value/taxable_amount
    # as ints (902715) where the schema wants strings — accept and stringify them.
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    work_order_number: str = Field(default="", description="Work Order No, e.g. 'M2511251'.")
    project_number: str = Field(default="", description="Project No, e.g. 'S250694GNKL'.")
    project_name: str = Field(default="", description="Project Name.")
    date_issued: str = Field(default="", description="The 'Date' of the work order, as printed (e.g. '11-FEB-2026').")
    wo_total_value: str = Field(default="", description="Grand Total (in Rs.) — the final total incl. taxes, DIGITS ONLY (strip commas), e.g. '902715'.")
    tender_number: str = Field(default="", description="The Empanelment No, e.g. '10(32)2021-AEOLOGIC(Tier-3)-Rev1'.")
    valid_till_date: str = Field(default="", description="The 'Valid Till:' date, as printed (e.g. '30/09/2026').")
    pi_number: str = Field(default="", description="PI Number — usually blank; empty string if absent.")
    user_contact_detail: str = Field(
        default="",
        description="The Project Manager NAME from the line 'the concerned Project Manager "
        "(<name>, <title>) at NICSI…' — NOT the 'Issued to' agency contact.",
    )
    doc_type: str = Field(default="work_order", description="Always 'work_order'.")
    tender_type: str = Field(
        default="",
        description="The work-order category, read from the line-item descriptions/HSN: "
        "'tier_3' = 'Level N … Tier 3' (HSN 998314, tender contains '(Tier-3)'); "
        "'support_engineer' = 'Software Application Support Engineer' (HSN 998313); "
        "'gis' = 'GIS Digitization …' (HSN 998319).",
    )
    items: list[WorkOrderItem] = Field(default_factory=list, description="The line items from the order table.")
    taxable_amount: str = Field(default="", description="'Total Amount in Rs.' — the sum of line taxable amounts BEFORE taxes, DIGITS ONLY, e.g. '765013'.")
    ai_score: AIScore = Field(default=0, description=_AI_SCORE_DESC)


# ---------------------------------------------------------------------------
# Payment Advice documents (RTGS/NEFT 'Transfer of Fund' letters)
# ---------------------------------------------------------------------------

class PaymentBill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bill_no: str = Field(default="", description="The 'Bill No' of the row, e.g. 'AEO/26-27/017170'. Reassemble if it wraps across lines.")
    work_order: str = Field(default="", description="The 'PO. No.' column — the work-order M-number, e.g. 'M2602089'. NOT the 'Project No.' (the S…/C… code).")


class PaymentAdvice(BaseModel):
    """A NICSI Payment Advice → the net amount paid, the advice date, and the list
    of enclosed bills (each mapped to its work order)."""

    model_config = ConfigDict(extra="ignore")

    pa_amount: int = Field(default=0, description="Net amount transferred — the 'Payment being made' grand total on the Total row (AFTER TDS & GST-TDS), digits only.")
    pa_date: str = Field(default="", description="The advice/letter date as DD-MON-YYYY (expand a 2-digit year, e.g. '26-MAY-26' → '26-MAY-2026').")
    bills: list[PaymentBill] = Field(default_factory=list, description="One entry per enclosed bill row in the table.")
    ai_score: AIScore = Field(default=0, description=_AI_SCORE_DESC)


# ---------------------------------------------------------------------------
# EPF Form 11 (Declaration Form) — member identity + KYC details
# ---------------------------------------------------------------------------

class Form11(BaseModel):
    """EPFO 'New Form No. 11 - Declaration Form' → the member's identity + KYC
    fields. The form is hand-filled and scanned, so the Field descriptions double
    as read instructions for the vision model."""

    model_config = ConfigDict(extra="ignore")

    employee_name: str = Field(default="", description="Item 1 'Name of Member (Aadhar Name)', exactly as written.")
    uan_no: str = Field(default="", description="The 'Universal Account Number (UAN)' (a 12-digit number; digits only).")
    aadhar_no: str = Field(default="", description="KYC 'AADHAR Number' (a 12-digit number; strip spaces → digits only).")
    email: str = Field(default="", description="The 'eMail ID' on the form.")
    phone: str = Field(default="", description="The 'Mobile No' (digits only).")
    account_no: str = Field(default="", description="KYC 'Bank Account No.' (digits only).")
    ifsc: str = Field(default="", description="KYC 'IFS Code', e.g. 'SBIN0020980' (uppercase letters+digits, no spaces).")
    pan_no: str = Field(default="", description="KYC 'Permanent Account Number (PAN)', a 10-char alphanumeric code, uppercase.")
    ai_score: AIScore = Field(default=0, description=_AI_SCORE_DESC)
