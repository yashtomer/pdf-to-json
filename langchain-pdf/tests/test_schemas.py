"""Tests for the ai_score field + clamping shared across all schemas.

Runs standalone (`python tests/test_schemas.py`) or under pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas import Form11, MPRRecord, PaymentAdvice, WorkOrder  # noqa: E402


def _mpr(score):
    return MPRRecord(work_order="X", mpr_month="April 2026", ai_score=score, employees=[])


def test_ai_score_clamps_out_of_range():
    assert _mpr(150).ai_score == 100   # the user's "hundred fifty" can't escape
    assert _mpr(-5).ai_score == 0


def test_ai_score_coerces_strings_and_floats():
    assert _mpr("95").ai_score == 95
    assert WorkOrder(ai_score=95.6).ai_score == 96
    assert PaymentAdvice(ai_score="oops").ai_score == 0   # junk -> 0 (needs review)


def test_ai_score_defaults_to_zero_when_absent():
    assert Form11().ai_score == 0
    assert WorkOrder().ai_score == 0


def test_ai_score_is_serialized():
    assert _mpr(80).model_dump()["ai_score"] == 80
    assert Form11(ai_score=72).model_dump()["ai_score"] == 72


def _sig(month, sig):
    return MPRRecord(work_order="X", mpr_month=month, signature_date=sig, employees=[]).signature_date


def test_signature_date_kept_when_year_matches():
    assert _sig("June 2026", "02/07/2026") == "02/07/2026"   # Claude/Gemini read
    assert _sig("December 2026", "05/01/2027") == "05/01/2027"  # signed next Jan, within 1yr


def test_signature_date_blanked_when_year_implausible():
    assert _sig("June 2026", "06/21/2024") == ""             # Groq misread -> blanked
    assert _sig("June 2026", "02/07/2029") == ""


def test_signature_date_left_alone_when_unverifiable():
    assert _sig("", "02/07/2026") == "02/07/2026"            # no month -> can't judge
    assert _sig("June 2026", "02/07/26") == "02/07/26"       # 2-digit year -> leave it


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
