"""PDF -> grouped MPR JSON, using Claude (via LangChain) to read the page images.

One call per document: every page image is sent together so the model can resolve
continuation pages, multi-month splits and multi-work-order tables holistically —
the things that needed a lot of brittle parsing code in the Surya version.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pdf2image import convert_from_path
from PIL import Image

from .config import settings
from .schemas import MPRRecord, MPRDocument


def load_page_images(path: Path) -> list[Image.Image]:
    """Load a document's pages as PIL images. Handles BOTH PDFs (rendered with
    pdf2image) and image files (jpg/png/…), detected by content — so a phone photo
    of an MPR works just like a PDF."""
    data = path.read_bytes()
    if data[:5] == b"%PDF-":
        return convert_from_path(str(path), dpi=settings.pdf_dpi)
    return [Image.open(io.BytesIO(data))]

# Domain knowledge for NICSI MPRs, learned the hard way on the Surya version.
SYSTEM_PROMPT = """\
You read NICSI MPR (Monthly Performance Report) documents and return structured
data. You are given the page images of ONE document (possibly several pages).

Return one record per (work_order, mpr_month). For each employee capture:
employee_name, designation (full text), and leaves (a number; halves allowed).

Follow these rules exactly:
1. WORK ORDER per row: if a table has a 'Work Order No.' / 'Wos' column, each row
   may belong to a DIFFERENT work order — split into one record per work order.
   Otherwise use the work order printed on the page/footer for all its rows.
2. LEAVES: use the 'Total Absence' / 'Leaves Taken' / 'Absent' count. It is NOT
   the Remarks text, NOT a date, and NOT an attendance time like '07:44'. A cell
   like '2 (02.01.2026 & 19.01.2026)' means 2. '-' or blank means 0. Halves like
   0.5 / 1.5 are valid.
3. MULTI-MONTH: if the month is a range like 'January to March 2026' and the
   document includes per-employee Leave Adjustment Certificates, output one record
   per month. For each employee, in each month, count ONLY the leave entries whose
   DATES fall in that specific month, taken from THAT employee's certificate. Do
   not reuse a value across months, and do not split the combined MPR total evenly.
   A month with no leave entry for that employee is 0. Sum half-days as written
   (e.g. two 0.5-day leaves in a month = 1.0; one 0.5-day = 0.5). Re-check each
   value against the certificate dates before answering.
4. GROUPED NAMES: if one cell lists several people as a numbered list
   ('1. A 2. B 3. C') sharing one designation, output one employee per name.
5. CONTINUATION PAGES: a table that continues onto the next page (same work order,
   no new month) belongs to the same record — merge them; do not duplicate.
6. Ignore 'Justification for Attendance not marked' detail tables (date/day/reason)
   — they are not employees.
7. Names may be ALL-CAPS — keep them as printed. If a name is truly unreadable,
   use an empty string; never invent names or numbers.
8. Format mpr_month as 'Month YYYY' (e.g. 'April 2026').
"""


def _downscale(img):
    """Cap the long edge to image_max_edge so we don't pay for resolution above
    what Claude uses (it downscales internally anyway). Saves image tokens."""
    longest = max(img.size)
    if longest <= settings.image_max_edge:
        return img
    scale = settings.image_max_edge / longest
    new_size = (round(img.size[0] * scale), round(img.size[1] * scale))
    return img.resize(new_size)


def _pdf_to_image_blocks(pdf_path: Path) -> list[dict]:
    """Render PDF/image pages to base64 (JPEG) image blocks for the chat message."""
    images = load_page_images(pdf_path)
    blocks: list[dict] = []
    for img in images[: settings.max_pages]:
        img = _downscale(img.convert("RGB"))
        buf = io.BytesIO()
        # JPEG (q85) is far smaller than PNG for scanned pages — same tokens to
        # Claude (token cost is by pixel size, not bytes) but much less upload.
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )
    return blocks


def _system_message() -> SystemMessage:
    """System prompt, optionally marked for prompt caching.

    NOTE: Anthropic only caches a prefix once it exceeds the minimum cacheable
    size (~1024 tokens for Sonnet). Our rules prompt is shorter than that today,
    so caching is a no-op until the prompt grows (e.g. if few-shot examples are
    added). The structure is correct and costs nothing extra when it doesn't hit.
    """
    if settings.enable_prompt_cache:
        return SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
    return SystemMessage(content=SYSTEM_PROMPT)


def _llm() -> ChatAnthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to the .env file and restart."
        )
    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        max_tokens=settings.anthropic_max_tokens,
        timeout=settings.anthropic_timeout,
        temperature=settings.anthropic_temperature,
    )


def extract_grouped(pdf_path: Path) -> list[MPRRecord]:
    """Extract a NICSI MPR PDF into the grouped record list."""
    image_blocks = _pdf_to_image_blocks(pdf_path)
    if not image_blocks:
        return []

    content = [
        {"type": "text", "text": "Extract the MPR data from these page images."},
        *image_blocks,
    ]
    structured = _llm().with_structured_output(MPRDocument)
    result: MPRDocument = structured.invoke(
        [_system_message(), HumanMessage(content=content)]
    )
    return _merge_by_work_order_month(result.records)


def _merge_by_work_order_month(records: list[MPRRecord]) -> list[MPRRecord]:
    """Consolidate records that share the same (work_order, mpr_month) into one,
    concatenating their employees. The model sometimes emits one record per row
    when a work order spans several rows; this groups them back together (order
    preserved). Distinct work orders / months stay separate."""
    merged: list[MPRRecord] = []
    index: dict[tuple, MPRRecord] = {}
    for r in records:
        key = (r.work_order, r.mpr_month)
        if key in index:
            index[key].employees.extend(r.employees)
        else:
            index[key] = r
            merged.append(r)
    return merged
