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
