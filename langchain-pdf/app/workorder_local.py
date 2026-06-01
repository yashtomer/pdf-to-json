"""Work Order PDF → structured JSON using a LOCAL LLM (Ollama, e.g. qwen2.5:14b).

Same task as workorder.py but the model runs on the box (Ollama) instead of the
Anthropic API — free + private, but on a CPU host it is SLOW (minutes/doc). This
uses the exact recipe that scored 100% in benchmarking: pdftotext → an explicit
prompt → Ollama /api/generate with format=json, temperature 0 → validate.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from .config import settings
from .schemas import WorkOrder
from .workorder import _pdf_text

# Explicit prompt (incl. the designation_level rule) — local models need the
# numeric rules spelled out; this is what took qwen2.5:14b from 21/23 to 23/23.
LOCAL_PROMPT = """You extract data from a NICSI Work Order. Return ONLY JSON with these keys/rules:
- work_order_number, project_number, project_name: as printed.
- date_issued: the Date, EXACTLY as printed (e.g. "11-FEB-2026"); do NOT reformat.
- tender_number: the Empanelment No.
- valid_till_date: the "Valid Till:" date, EXACTLY as printed (e.g. "30/09/2026").
- user_contact_detail: ONLY the Project Manager's NAME (e.g. "Neeraj Chawla") from "the concerned Project Manager (NAME, TITLE)". Name only — no title, no extra words.
- tender_type: "tier_3" if HSN 998314 / items say "Tier 3", else "support_engineer".
- wo_total_value: Grand Total, DIGITS ONLY (no commas).
- taxable_amount: Total Amount in Rs, DIGITS ONLY.
- items: array, one object PER TABLE ROW, each with: line_no, hsn_code, description, designation_level, manpower_count, period_text, start_date, end_date (deployment From/To, exactly as printed), unit_rate (THAT ROW's Unit Rate per Month, with decimals, no commas), line_total (THAT ROW's Total Amount, no commas).
- designation_level RULE: read the item's description. If it starts with "Level <number>" (e.g. "Level 7 (Minimum work experience...)"), set designation_level to that number as an INTEGER (e.g. 7, or 9 for "Level 9"). Only set it to null when the description has no "Level" (e.g. "Software Application Support Engineer").
IMPORTANT: each row has its OWN unit_rate and line_total — do NOT copy row 1's numbers into row 2. Strip thousands commas (3,40,005.60 -> 340005.60).

WORK ORDER TEXT:
"""


def extract_workorder_local(pdf_path: Path) -> dict:
    """Returns {'result': WorkOrder, 'model': str, 'seconds': float}."""
    text = _pdf_text(pdf_path)
    body = json.dumps({
        "model": settings.ollama_model,
        "prompt": LOCAL_PROMPT + text,
        "format": "json",
        "stream": False,
        # num_ctx MUST be large enough to hold the whole work-order text, or
        # Ollama silently truncates it (default is ~2k) and the model loses the
        # table → empty/partial output. Work orders are ~5-6k tokens.
        "options": {"temperature": 0, "num_predict": 2000, "num_ctx": 16384},
    }).encode()
    url = settings.ollama_base_url.rstrip("/") + "/api/generate"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    t = time.time()
    resp = json.load(urllib.request.urlopen(req, timeout=settings.ollama_timeout))
    seconds = time.time() - t

    data = json.loads(resp["response"])
    return {"result": WorkOrder(**data), "model": settings.ollama_model, "seconds": round(seconds, 1)}
