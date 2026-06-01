"""Pydantic schemas — also drive Claude's structured output.

The Field descriptions are sent to the model (via tool-calling), so they double
as extraction instructions. Keep them precise.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    line_no: int = Field(description="The S.No of the line item (1, 2, ...).")
    hsn_code: str = Field(description="The HSN/SAC code, e.g. '998314' (tier_3) or '998313' (support_engineer).")
    description: str = Field(description="The full item description text.")
    designation_level: int | None = Field(
        default=None,
        description="For tier_3 rows the number N from 'Level N' (e.g. 7). null for "
        "support_engineer rows (which have no Level).",
    )
    manpower_count: int = Field(description="No. of Persons Required (column A).")
    period_text: str = Field(description="The Required Period text (column B), e.g. 'Five Month(s)'.")
    start_date: str = Field(description="Deployment start date (the 'From' of column D), as printed (DD/MM/YYYY).")
    end_date: str = Field(description="Deployment end date (the 'To' of column D), as printed.")
    unit_rate: float = Field(description="Unit Rate per Month excluding taxes (column C), digits only.")
    taxable_amount: float = Field(description="Line Total Amount excluding taxes (column E = A×B×C).")
    line_total: float = Field(description="Same as the line's Total Amount (column E).")


class WorkOrder(BaseModel):
    """A NICSI Work Order document → structured fields. Also the tool schema, so the
    Field descriptions instruct the model."""

    work_order_number: str = Field(description="Work Order No, e.g. 'M2511251'.")
    project_number: str = Field(description="Project No, e.g. 'S250694GNKL'.")
    project_name: str = Field(description="Project Name.")
    date_issued: str = Field(description="The 'Date' of the work order, as printed (e.g. '11-FEB-2026').")
    wo_total_value: str = Field(description="Grand Total (in Rs.) — the final total incl. taxes, DIGITS ONLY (strip commas), e.g. '902715'.")
    tender_number: str = Field(description="The Empanelment No, e.g. '10(32)2021-AEOLOGIC(Tier-3)-Rev1'.")
    valid_till_date: str = Field(description="The 'Valid Till:' date, as printed (e.g. '30/09/2026').")
    pi_number: str = Field(description="PI Number — usually blank; empty string if absent.")
    user_contact_detail: str = Field(
        description="The Project Manager NAME from the line 'the concerned Project Manager "
        "(<name>, <title>) at NICSI…' — NOT the 'Issued to' agency contact.",
    )
    doc_type: str = Field(default="work_order", description="Always 'work_order'.")
    tender_type: str = Field(
        description="'tier_3' if line items are 'Level N … Tier 3' (HSN 998314, tender contains "
        "'(Tier-3)'); 'support_engineer' if line items are 'Software Application Support Engineer' "
        "(HSN 998313).",
    )
    items: list[WorkOrderItem] = Field(description="The line items from the order table.")
    taxable_amount: str = Field(description="'Total Amount in Rs.' — the sum of line taxable amounts BEFORE taxes, DIGITS ONLY, e.g. '765013'.")
