"""Payment Advice PDF → structured JSON, using Claude on the extracted TEXT.

A NICSI Payment Advice is the RTGS/NEFT "Transfer of Fund" letter that encloses a
list of bills being paid in one transfer. We pull out the net amount transferred,
the advice date, and the per-bill (bill_no, work_order) mapping. Like work orders
these are digital (text) PDFs, so we read layout text with `pdftotext -layout` and
fall back to page images only for scans.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .extractor import _pdf_to_image_blocks  # reuse the image renderer for the fallback
from .schemas import PaymentAdvice
from .workorder import _llm, _pdf_text  # reuse the shared Claude client + text extractor

SYSTEM_PROMPT = """\
You read NICSI **Payment Advice** PDFs — an RTGS/NEFT 'Transfer of Fund' letter that
encloses a table of the bills being paid in one transfer — and return the structured
fields. You are given the extracted text (and, as a fallback, the page images).

- pa_date = the letter date near the top (e.g. '26-MAY-26'). Expand a 2-digit year
  to four digits (26 → 2026) and output as DD-MON-YYYY.
- pa_amount = the net amount actually transferred: the 'Payment being made' grand
  total on the 'Total' row — the right-most total, AFTER the TDS and GST-TDS
  deductions (NOT the gross 'Amount (Rs)' total). DIGITS ONLY (strip commas). In a
  document whose gross total is 946209 and TDS/GST-TDS are 16038 each, pa_amount is
  914133.
- bills = one entry for EVERY row of the enclosed table:
    - bill_no = the 'Bill No' (e.g. 'AEO/26-27/017170'). The text often wraps the
      bill number across two lines — reassemble it into one value.
    - work_order = the 'PO. No.' column (the work-order M-number, e.g. 'M2602089').
      It may wrap across lines — reassemble it. This is NOT the 'Project No.' (the
      S…/C… code in the neighbouring column).

Read values exactly as printed; never invent. Include every bill row in order.
"""


def _invoke_claude(content) -> PaymentAdvice:
    structured = _llm().with_structured_output(PaymentAdvice)
    return structured.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=content)])


def run_payment_advice(pdf_path: Path, invoke) -> PaymentAdvice:
    """Shared pipeline. `invoke(content) -> PaymentAdvice` is the model call.
    Digital PDFs use one text pass; scans fall back to page images."""
    text = _pdf_text(pdf_path)
    if len(text.strip()) >= 200:
        content: list = [
            {"type": "text", "text": "Extract the payment advice. Here is its text:\n\n" + text}
        ]
    else:
        content = [
            {"type": "text", "text": "Extract the payment advice from these page images."},
            *_pdf_to_image_blocks(pdf_path),
        ]
    return invoke(content)


def extract_payment_advice(pdf_path: Path) -> PaymentAdvice:
    return run_payment_advice(pdf_path, _invoke_claude)
