"""
Surya 2 (v0.20.x) PDF extractor.

Surya 2 is a single 650M vision-language model that does layout + OCR + table
recognition in one pass, served through an inference backend:
  - llama.cpp `llama-server` on CPU / Apple Silicon
  - vLLM on NVIDIA GPU

We use full-page recognition: one VLM call per page returns a list of `blocks`,
each with a layout `label` and its content as `html` (tables come back as
`<table>...</table>`). That HTML is what the grouper consumes.

The SuryaInferenceManager auto-spawns the backend on first use (or attaches to
SURYA_INFERENCE_URL if set).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from PIL import Image
from pdf2image import convert_from_path

# Allow PIL to handle huge scanner images without the decompression-bomb warning
Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------------------
# Idempotent model-download patch (carried over from v1 — Surya's downloader
# uses shutil.move and errors if a partial file already exists in the target).
# ---------------------------------------------------------------------------
def _install_idempotent_move() -> None:
    import surya.common.s3 as _s3  # type: ignore

    _orig = _s3.shutil.move

    def _move(src: str, dst: str, *a: Any, **k: Any) -> str:
        target = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
        if os.path.exists(target):
            shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
        return _orig(src, dst, *a, **k)

    _s3.shutil.move = _move


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Serialize a Surya 2 block object into a plain dict (defensive getattr)."""
    bbox = getattr(block, "bbox", None)
    return {
        "label": getattr(block, "label", None),
        "raw_label": getattr(block, "raw_label", None),
        "html": getattr(block, "html", "") or "",
        "bbox": [round(float(c), 2) for c in bbox] if bbox else None,
        "confidence": getattr(block, "confidence", None),
        "reading_order": getattr(block, "reading_order", getattr(block, "position", None)),
        "skipped": getattr(block, "skipped", False),
        "error": getattr(block, "error", False),
    }


class SuryaExtractor:
    """Loads the Surya 2 inference manager + recognition predictor once."""

    def __init__(self) -> None:
        try:
            _install_idempotent_move()
        except Exception:
            pass

        from surya.inference import SuryaInferenceManager
        from surya.recognition import RecognitionPredictor

        print("[surya2] Starting inference manager (spawns llama-server) …")
        self.manager = SuryaInferenceManager()
        self.recognition = RecognitionPredictor(self.manager)
        print("[surya2] Ready.")

    def extract_from_pdf(self, pdf_path: str | Path, dpi: int = 150) -> dict[str, Any]:
        pdf_path = Path(pdf_path)
        images = convert_from_path(str(pdf_path), dpi=dpi)
        pages = [
            {"page_number": i, **self.extract_from_image(img)}
            for i, img in enumerate(images, start=1)
        ]
        return {
            "file": pdf_path.name,
            "page_count": len(images),
            "dpi": dpi,
            "pages": pages,
        }

    def extract_from_image(self, image: Image.Image) -> dict[str, Any]:
        """Full-page OCR: one VLM call → blocks (each with label + html)."""
        result = self.recognition([image])[0]
        blocks = [_block_to_dict(b) for b in getattr(result, "blocks", [])]
        return {"blocks": blocks}
