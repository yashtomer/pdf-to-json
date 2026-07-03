"""MPR PDF → grouped JSON using a LOCAL VISION LLM (Ollama, e.g. qwen3-vl).

Same shape as /extract-grouped (Claude) but the page images are read by a local
vision model via Ollama — free + private. Renders pages to images, sends them to
Ollama /api/generate with the MPR rules + format=json, then reuses the same
merge/cleanup as the Claude path.
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path

from .config import settings
from .extractor import SYSTEM_PROMPT, _merge_by_work_order_month, load_page_images
from .mpr_reconcile import reconcile_multimonth_leaves
from .schemas import MPRDocument
from .workorder import _pdf_text

_JSON_INSTRUCTION = (
    'Return ONLY a JSON object (no markdown fences, no prose) of this exact shape: '
    '{"records": [{"work_order": "string", "mpr_month": "Month YYYY", '
    '"signature_date": "string", "ai_score": 0, "employees": [{"employee_name": '
    '"string", "designation": "string", "leaves": 0}]}]}. signature_date is the date '
    'by the reporting officer signature/stamp; ai_score is an integer 0-100: your '
    'confidence that that record is 100% correct.'
)


def _parse_json(text: str) -> dict:
    """Pull the JSON object out of the model's reply (tolerates fences / prose)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b != -1:
        t = t[a:b + 1]
    return json.loads(t)


# Vision models spend many tokens per image; cap a bit smaller than the Claude
# path so multi-page MPRs fit the local model's context.
_VISION_MAX_EDGE = 1300


def _pdf_to_base64_images(pdf_path: Path) -> list[str]:
    images = load_page_images(pdf_path)
    out: list[str] = []
    for img in images[: settings.max_pages]:
        img = img.convert("RGB")
        img.thumbnail((_VISION_MAX_EDGE, _VISION_MAX_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


def extract_grouped_vision(pdf_path: Path) -> dict:
    """Returns {'model': str, 'seconds': float, 'records': [...]}.."""
    images = _pdf_to_base64_images(pdf_path)
    # Size the context to the page count (each page image is several thousand
    # tokens) — too small a num_ctx makes the model return empty on multi-page MPRs.
    num_ctx = min(12000 + len(images) * 10000, 131072)
    # NOTE: no "format": "json" — Ollama's constrained JSON decoding makes qwen3-vl
    # return empty; instead we ask for JSON in the prompt and parse it ourselves.
    body = json.dumps({
        "model": settings.ollama_vision_model,
        "system": SYSTEM_PROMPT,
        "prompt": "Extract the MPR data from these page images. " + _JSON_INSTRUCTION,
        "images": images,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 6000, "num_ctx": num_ctx},
    }).encode()

    import urllib.request
    url = settings.ollama_base_url.rstrip("/") + "/api/generate"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t = time.time()
    resp = json.load(urllib.request.urlopen(req, timeout=settings.ollama_timeout))
    seconds = time.time() - t

    data = _parse_json(resp["response"])
    records = MPRDocument(**data).records
    records = _merge_by_work_order_month(records)
    # Deterministically fix the per-month split for multi-month MPRs (see groq.py).
    records = reconcile_multimonth_leaves(_pdf_text(pdf_path), records)
    # drop rows whose name never came through (mirror the Claude path's cleanup)
    for r in records:
        r.employees = [e for e in r.employees if e.employee_name]
    return {
        "model": settings.ollama_vision_model,
        "seconds": round(seconds, 1),
        "records": [r.model_dump() for r in records],
    }
