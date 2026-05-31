"""Bulk MPR extraction via Anthropic's Message Batches API — 50% cheaper.

Use this for your monthly run (hundreds of MPRs at once). It submits ALL PDFs as
one async batch (half the per-token price), polls until done, and writes one JSON
per PDF. Prompt caching is on, so the shared system prompt is billed once.

Usage:
    python batch_extract.py <input_folder> [output_folder]

Example:
    python batch_extract.py ../samples out/
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
from pathlib import Path

import anthropic
from pdf2image import convert_from_path

from app.config import settings
from app.extractor import SYSTEM_PROMPT, _downscale
from app.schemas import MPRDocument

_TOOL = {
    "name": "emit_mpr",
    "description": "Return the extracted MPR records.",
    "input_schema": MPRDocument.model_json_schema(),
}


def _build_request(custom_id: str, pdf_path: Path) -> dict:
    """One batch request for a single PDF (all its pages as images)."""
    images = convert_from_path(str(pdf_path), dpi=settings.pdf_dpi)
    content: list[dict] = [
        {"type": "text", "text": "Extract the MPR data from these page images."}
    ]
    for img in images[: settings.max_pages]:
        img = _downscale(img.convert("RGB"))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(buf.getvalue()).decode(),
                },
            }
        )

    system = [{"type": "text", "text": SYSTEM_PROMPT}]
    if settings.enable_prompt_cache:
        system[0]["cache_control"] = {"type": "ephemeral"}

    return {
        "custom_id": custom_id,
        "params": {
            "model": settings.anthropic_model,
            "max_tokens": settings.anthropic_max_tokens,
            "system": system,
            "tools": [_TOOL],
            "tool_choice": {"type": "tool", "name": "emit_mpr"},
            "messages": [{"role": "user", "content": content}],
        },
    }


def _records_from_message(message) -> list:
    """Pull the tool_use input out of a completed message → list of records."""
    for block in message.content:
        if block.type == "tool_use":
            return MPRDocument(**block.input).model_dump()["records"]
    return []


def main(in_folder: str, out_folder: str = "out") -> None:
    if not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY not set — add it to .env first.")

    pdfs = sorted(Path(in_folder).glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {in_folder}")
    out = Path(out_folder)
    out.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    print(f"Building {len(pdfs)} requests…")
    requests = [_build_request(p.stem, p) for p in pdfs]

    batch = client.messages.batches.create(requests=requests)
    print(f"Submitted batch {batch.id} — polling (this runs async, may take a while)…")

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        counts = batch.request_counts
        print(f"  status={batch.processing_status}  "
              f"succeeded={counts.succeeded} errored={counts.errored} "
              f"processing={counts.processing}")
        if batch.processing_status == "ended":
            break
        time.sleep(15)

    ok = err = 0
    for entry in client.messages.batches.results(batch.id):
        cid = entry.custom_id
        if entry.result.type == "succeeded":
            records = _records_from_message(entry.result.message)
            (out / f"{cid}.json").write_text(
                json.dumps(records, indent=2, ensure_ascii=False)
            )
            ok += 1
        else:
            (out / f"{cid}.ERROR.json").write_text(json.dumps(
                {"custom_id": cid, "result": str(entry.result)}, indent=2))
            err += 1

    print(f"\nDone. {ok} ok, {err} errored → {out}/")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out")
