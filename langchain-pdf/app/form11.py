"""EPF Form 11 (Declaration Form) → structured JSON, using Claude on the page image.

The EPFO "New Form No. 11 - Declaration Form" is the composite declaration a new
employee fills on joining — it carries the member's identity and KYC details (UAN,
Aadhaar, bank account + IFSC, PAN, email, phone). These forms are hand-filled and
scanned/photographed, so there is no extractable text: we send the page image(s)
to Claude and read the fields directly, exactly like the MPR vision path.
"""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .config import settings
from .extractor import _llm, _pdf_to_image_blocks  # reuse the vision client + image renderer
from .schemas import Form11

_NON_DIGITS = re.compile(r"\D+")
_WHITESPACE = re.compile(r"\s+")

# A PAN is exactly AAAAA9999A — five letters, four digits, then one letter. When a
# handwritten read doesn't fit that shape it's almost always a digit/letter that
# look alike (S↔5, O↔0, I↔1…). We retry each character through the confusion map
# for the class its position expects, and keep the result only if it becomes a
# valid PAN — so a correct PAN is never altered.
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}
_TO_DIGIT = {"O": "0", "I": "1", "L": "1", "Z": "2", "A": "4", "S": "5", "G": "6", "B": "8"}


def _fix_pan(pan: str) -> str:
    if not pan or len(pan) != 10 or _PAN_RE.match(pan):
        return pan
    fixed = "".join(
        (_TO_LETTER if (i < 5 or i == 9) else _TO_DIGIT).get(ch, ch)
        for i, ch in enumerate(pan)
    )
    return fixed if _PAN_RE.match(fixed) else pan

SYSTEM_PROMPT = """\
You read an EPFO **Form 11 (Declaration Form)** — the "New Form No. 11" Employees'
Provident Fund composite declaration a new employee fills on joining — and return
the member's identity + KYC fields. The form is hand-filled and scanned, so read
the handwriting carefully. Return each value EXACTLY as written; never invent. Use
an empty string for any field that is genuinely blank or unreadable.

Field locations on the form:
- employee_name = item 1 'Name of Member (Aadhar Name)'.
- email = the 'eMail ID' item.
- phone = the 'Mobile No' item (digits only).
- uan_no = the 'Universal Account Number (UAN)' under 'Previous Employment details'
  (a 12-digit number).
- In the 'KYC Details' block (item 12):
    - account_no = the 'Bank Account No.' (the number written before the IFS code).
    - ifsc = the 'IFS Code' next to that account (e.g. 'SBIN0020980').
    - aadhar_no = the 'AADHAR Number' (a 12-digit number).
    - pan_no = the 'Permanent Account Number (PAN)' (a 10-char alphanumeric code).

For the numeric fields (phone, uan_no, account_no, aadhar_no) output digits only —
strip any spaces, dashes or separators between groups. Output ifsc and pan_no in
uppercase with no spaces.

Set ai_score to your confidence (an integer 0-100 percent) that you read the whole
form 100% correctly — handwriting that is hard to read should lower it.
"""


def _invoke_claude(content) -> Form11:
    structured = _llm().with_structured_output(Form11)
    return structured.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=content)])


def _normalize(f: Form11) -> Form11:
    """Deterministic clean-up so a model's formatting slips never reach the caller.
    These fields have a fixed shape: the identifier numbers are digits only, an
    email never contains spaces, and IFSC/PAN are uppercase codes. (employee_name is
    left untouched — it legitimately contains spaces.)"""
    f.email = _WHITESPACE.sub("", f.email or "")
    f.phone = _NON_DIGITS.sub("", f.phone or "")
    f.uan_no = _NON_DIGITS.sub("", f.uan_no or "")
    f.aadhar_no = _NON_DIGITS.sub("", f.aadhar_no or "")
    f.account_no = _NON_DIGITS.sub("", f.account_no or "")
    f.ifsc = _WHITESPACE.sub("", f.ifsc or "").upper()
    f.pan_no = _fix_pan(_WHITESPACE.sub("", f.pan_no or "").upper())
    return f


def run_form11(pdf_path: Path, invoke) -> Form11:
    """Vision pipeline: a Form 11 is always a hand-filled scan, so we read the page
    image(s). `invoke(content) -> Form11` is the model call. The result is normalized
    deterministically (digits-only IDs, space-free email/codes)."""
    content = [
        {"type": "text", "text": "Extract the Form 11 fields from these page images."},
        *_pdf_to_image_blocks(pdf_path),
    ]
    return _normalize(invoke(content))


def extract_form11(pdf_path: Path) -> Form11:
    return run_form11(pdf_path, _invoke_claude)


def _invoke_groq(content) -> Form11:
    """Same prompt + schema as the Claude path, but read by a vision-capable Llama 4
    on Groq. The image blocks are data-URL base64, which Groq accepts as-is."""
    from langchain_groq import ChatGroq

    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to the .env file and restart.")
    llm = ChatGroq(model=settings.groq_model, api_key=settings.groq_api_key, temperature=0)
    # json_schema (constrained decoding) over the default tool-calling — Groq's
    # tool-calling 400s on strict schema type mismatches; this forces clean types.
    structured = llm.with_structured_output(Form11, method="json_schema")
    return structured.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=content)])


def extract_form11_groq(pdf_path: Path) -> Form11:
    return run_form11(pdf_path, _invoke_groq)


_NVIDIA_F11_SHAPE = (
    'Return ONLY a JSON object (no markdown fences, no prose) with exactly these keys: '
    '{"employee_name":"","uan_no":"","aadhar_no":"","email":"","phone":"","account_no":"",'
    '"ifsc":"","pan_no":"","ai_score":0}'
)


def _invoke_nvidia(content) -> Form11:
    """Read the Form 11 via NVIDIA NIM. NIM vision models are unreliable with
    structured-output tool-calling, so we prompt for JSON and parse it (like the local
    paths) — via the shared helper in nvidia.py."""
    from .nvidia import nvidia_json_invoke

    return Form11(**nvidia_json_invoke(SYSTEM_PROMPT, content, _NVIDIA_F11_SHAPE))


def extract_form11_nvidia(pdf_path: Path) -> Form11:
    return run_form11(pdf_path, _invoke_nvidia)
