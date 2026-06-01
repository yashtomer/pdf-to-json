"""Gemini backends for MPR (vision) and Work Order (text).

Mirrors the Claude paths (extractor.py / workorder.py) but uses Google Gemini via
langchain-google-genai. Gemini Flash is cheap, has a generous free tier, and is
natively multimodal — so the *same* MPR image pipeline and Work Order text
pipeline work, just with a different model.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from .config import settings
from .extractor import SYSTEM_PROMPT as MPR_PROMPT
from .extractor import _merge_by_work_order_month, _pdf_to_image_blocks
from .schemas import MPRRecord, MPRDocument, WorkOrder
from .workorder import SYSTEM_PROMPT as WO_PROMPT
from .workorder import run_workorder


def _structured(schema):
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set. Add it to the .env file and restart.")
    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=0,
    )
    return llm.with_structured_output(schema)


def extract_grouped_gemini(pdf_path: Path) -> list[MPRRecord]:
    """MPR PDF -> grouped records, read from the page images by Gemini (vision)."""
    image_blocks = _pdf_to_image_blocks(pdf_path)
    if not image_blocks:
        return []
    content = [
        {"type": "text", "text": "Extract the MPR data from these page images."},
        *image_blocks,
    ]
    result: MPRDocument = _structured(MPRDocument).invoke(
        [SystemMessage(content=MPR_PROMPT), HumanMessage(content=content)]
    )
    return _merge_by_work_order_month(result.records)


def extract_workorder_gemini(pdf_path: Path) -> WorkOrder:
    """Work Order PDF -> structured fields via Gemini. Text for digital PDFs, page
    images (+ majority vote) for scanned ones — same shared pipeline as Claude."""
    def invoke(content):
        return _structured(WorkOrder).invoke(
            [SystemMessage(content=WO_PROMPT), HumanMessage(content=content)]
        )
    return run_workorder(pdf_path, invoke)
