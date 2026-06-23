"""Deterministic multi-month leave reconciliation.

NICSI MPRs that cover a RANGE of months (e.g. "April & May 2026") show only the
*combined* absence total in the summary table; the per-month split lives in each
employee's Leave Adjustment Certificate ("... 16.05.2026 (Half day), 27.05.2026
(One day)"). Vision models — especially Llama 4 Scout on the Groq path — read the
dates but mis-assign them to months. We redo that bucketing in Python.

This mirrors `reconcile_workorder`: the model extracts, then a deterministic layer
fixes the mechanical part. It is conservative — it only overrides an employee's
per-month leaves when the certificate parses cleanly AND its dated total matches
the total the model already produced for that employee. Otherwise it leaves the
model's values untouched, so it can never make a correct extraction worse.
"""
from __future__ import annotations

import calendar
import re

from .schemas import MPRRecord

_TITLES = {"mr", "mrs", "ms", "miss", "dr", "smt", "shri", "sri", "kumari"}
_WORD_NUM = {
    "half": 0.5, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
}
# Either a date like 16.05.2026, or a parenthetical like "(Half day)"/"(Two days)".
_TOKEN_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})|\(([^)]*?days?[^)]*?)\)", re.I)


def _words_to_number(phrase: str) -> float | None:
    """'Two days' -> 2.0, 'Half day' -> 0.5, 'one and half' -> 1.5. None if no
    number word is present. Number words joined by 'and'/'&' are summed."""
    total = 0.0
    found = False
    for part in re.split(r"\band\b|&", phrase.lower()):
        for word, value in _WORD_NUM.items():
            if re.search(rf"\b{word}\b", part):
                total += value
                found = True
                break
    return total if found else None


def _name_tokens(name: str) -> set[str]:
    """Significant lowercased name tokens — titles and single-letter initials
    dropped — so 'Mrs.Arunasankari.R' and 'R Arunasankari' share {'arunasankari'}."""
    return {t for t in re.findall(r"[a-z]+", name.lower()) if len(t) > 1 and t not in _TITLES}


def _parse_certificate(block: str) -> tuple[str, dict[str, float]] | None:
    """Parse one certificate block into (person_name, {'April 2026': leaves, ...}).

    Returns None if there is no name or no cleanly-paired dated leave. Each date is
    bucketed by its own month; a parenthetical total shared by several dates (e.g.
    '14.05 & 15.05 (Two days)') is split equally across them. The text layer can be
    slightly out of reading order, but each date still sits next to its own
    parenthetical, so an ordered token scan stays correct.
    """
    m = re.search(r"certifies that (.+?) has taken", block, re.I)
    if not m:
        return None
    name = m.group(1).strip()

    buckets: dict[str, float] = {}
    pending: list[str] = []  # month labels awaiting their parenthetical total
    for tok in _TOKEN_RE.finditer(block):
        if tok.group(1):  # a date DD.MM.YYYY
            month = int(tok.group(2))
            if not 1 <= month <= 12:
                return None
            pending.append(f"{calendar.month_name[month]} {tok.group(3)}")
        else:             # a "(... day(s))" parenthetical
            value = _words_to_number(tok.group(4))
            if value is None or not pending:
                continue
            share = value / len(pending)
            for month_label in pending:
                buckets[month_label] = buckets.get(month_label, 0.0) + share
            pending = []

    if pending or not buckets:   # an unpaired date, or nothing parsed -> not clean
        return None
    return name, buckets


def reconcile_multimonth_leaves(text: str, records: list[MPRRecord]) -> list[MPRRecord]:
    """Correct each employee's per-month leaves from the Leave Adjustment
    Certificates in `text`. No-op unless the document spans >1 month and a
    certificate parses cleanly with a total matching the model's. Mutates and
    returns `records`."""
    months = {r.mpr_month for r in records}
    if len(months) < 2:
        return records

    norm = re.sub(r"\s+", " ", text)
    blocks = re.split(r"Leave Adjustment Certificate", norm, flags=re.I)[1:]
    certs = [c for c in (_parse_certificate(b) for b in blocks) if c]
    if not certs:
        return records

    # Each distinct employee's total leaves as the model reported them — used to
    # confirm a certificate matches the right person and parsed completely.
    model_total: dict[str, float] = {}
    for r in records:
        for e in r.employees:
            model_total[e.employee_name] = model_total.get(e.employee_name, 0.0) + e.leaves

    for cert_name, buckets in certs:
        ctoks = _name_tokens(cert_name)
        if not ctoks:
            continue
        matches = [nm for nm in model_total if ctoks & _name_tokens(nm)]
        if len(matches) != 1:
            continue  # ambiguous or no match -> don't touch
        target = matches[0]
        if set(buckets) - months:
            continue  # certificate names a month not in this document -> suspect
        if abs(sum(buckets.values()) - model_total[target]) > 0.01:
            continue  # dated total disagrees with the model -> low confidence, skip
        for r in records:
            for e in r.employees:
                if e.employee_name == target:
                    e.leaves = buckets.get(r.mpr_month, 0.0)
    return records
