"""
Turn a Surya 2 extraction result into the grouped NICSI-MPR JSON shape:

[
  {"work_order": "M2602757", "mpr_month": "February 2026",
   "employees": [{"employee_name": "...", "designation": "...", "leaves": 0}, ...]},
  ...
]

Surya 2 returns, per page, a list of `blocks` each with a layout `label` and
its content as `html`. Tables arrive as `<table>...</table>`. We parse those
table blocks into rows, map columns to fields, and pull the header fields
(work order, month) from the page's text.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html or "")).strip()


class _TableParser(HTMLParser):
    """Collect <table> rows as lists of cell strings."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            # A <br> inside a cell separates stacked lines — keep them apart.
            self._cell.append(" ")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        # <br/> arrives here (self-closing), not via handle_starttag.
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_table(html: str) -> list[list[str]]:
    p = _TableParser()
    try:
        p.feed(html or "")
    except Exception:
        return []
    return [r for r in p.rows if any(c.strip() for c in r)]


# ---------------------------------------------------------------------------
# Field extraction / normalisation
# ---------------------------------------------------------------------------

_WORK_ORDER_RE = re.compile(r"\b(M\d{6,})\b")
_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s*[-,]?\s*(\d{4})",
    re.IGNORECASE,
)
_DESIG_HINTS = re.compile(
    r"software application|support engineer|\blevel\s*\d+|minimum work|"
    r"to less than|relevant experience|increment|tier",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")

# Column-header / boilerplate words a cell must NOT be, to qualify as a name.
_HEADER_WORDS = {
    "si", "no", "sno", "sl", "sr", "name", "designation", "date", "joining",
    "working", "period", "from", "to", "absent", "leaves", "leave", "taken",
    "employee", "project", "work", "order", "mpr", "month", "performance",
    "report", "remarks",
    # extra header/column-label words so two-word headers like "AGENCY Name",
    # "Date of Relieving", "New Designation", "Candidate Name", "Attendance not
    # marked Date" are recognised as headers (every word is a header word) and
    # never mistaken for a person name.
    "agency", "relieving", "relieved", "new", "wos", "wo", "details", "service",
    "services", "candidate", "manager", "reason", "attendance", "marked", "not",
    "avg", "stay", "hours", "absence", "total", "qty", "overall", "day", "days",
    "holidays", "attendance/", "justified", "justification",
}


def _normalize_designation(desig: str) -> str:
    if not desig:
        return ""
    s = " ".join(desig.split())
    s = re.sub(r'\b2["”\']\s*Increment', "2nd Increment", s)
    s = re.sub(r'(\d)["”\'](\s*Increment)', lambda m: f"{m.group(1)}nd{m.group(2)}", s)
    s = re.sub(r"(experience\))\s*-?\s*(\d+(?:st|nd|rd|th)\s+year)", r"\1 - \2", s)
    return " ".join(s.split())


def _leaves(cell: str) -> int:
    """Parse an Absent/Leaves cell safely.

    "-" / "" / "nil" → 0. A date (e.g. "30/04/2026") → 0 (it's the wrong
    column). Otherwise a 1-3 digit count.
    """
    cs = (cell or "").strip()
    if not cs or cs in ("-", "–", "—") or cs.lower() in ("nil", "na", "n/a"):
        return 0
    if _DATE_RE.search(cs):
        return 0
    m = re.fullmatch(r"\d{1,3}", cs)
    return int(cs) if m else 0


_CONNECTORS = {"of", "the", "for", "and", "&"}


# ---------------------------------------------------------------------------
# Multi-month MPRs + Leave Adjustment Certificates
# ---------------------------------------------------------------------------
# Some MPRs cover a RANGE of months (e.g. "MPR for the Month: January to March
# 2026") and give a single *combined* Absent total in the main table. The PDF
# then includes one "Leave Adjustment Certificate" page per employee that lists
# their leave dates. When both are present we split the range into one record
# per month and compute each employee's leaves for THAT month from the dates.

_MONTH_LIST = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(_MONTH_LIST)}
_MONTH_INDEX.update({m[:3].lower(): i + 1 for i, m in enumerate(_MONTH_LIST)})

_RANGE_RE = re.compile(
    r"([A-Z][a-z]+)\s+to\s+([A-Z][a-z]+)\s+(\d{4})", re.IGNORECASE
)
# A leave entry: a date, optionally "to <date>", then "(<n> day[s])" in parens.
_LEAVE_ENTRY_RE = re.compile(
    r"(\d{1,2})[.\-/](\d{1,2})[.\-/]\d{2,4}"
    r"(?:\s*to\s*\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})?"
    r"\s*\(([^)]*?day[^)]*?)\)",
    re.IGNORECASE,
)
_CERT_NAME_RE = re.compile(
    r"(?:Mr|Mrs|Ms)\b\.?\s*/?\s*(?:Mr|Mrs|Ms)?\b\.?\s+(.+?)\s+has\s+taken",
    re.IGNORECASE,
)
_NUMWORD = {
    "half": 0.5, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _fraction_to_days(frac: str) -> float:
    """'(one day)'→1, '(Half day)'→0.5, '(Two days)'→2, 'one and half'→1.5."""
    t = frac.lower().strip()
    m = re.search(r"(one|two|three|four|five)\s+and\s+half", t)
    if m:
        return _NUMWORD[m.group(1)] + 0.5
    if "half" in t:
        return 0.5
    for word, n in _NUMWORD.items():
        if re.search(rf"\b{word}\b", t):
            return float(n)
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group()) if m else 0.0


def _name_tokens(name: str) -> set[str]:
    """Significant (≥3-letter) lowercase words of a name, for fuzzy matching."""
    return {w.lower() for w in re.findall(r"[A-Za-z]{3,}", name or "")}


def _parse_leave_certificate(text: str) -> tuple[str, dict[int, float]]:
    """Parse one certificate → (employee_name, {month_number: total_days})."""
    nm = _CERT_NAME_RE.search(text)
    name = nm.group(1).strip().rstrip(".") if nm else ""
    by_month: dict[int, float] = {}
    for em in _LEAVE_ENTRY_RE.finditer(text):
        month = int(em.group(2))
        days = _fraction_to_days(em.group(3))
        by_month[month] = by_month.get(month, 0.0) + days
    return name, by_month


def _parse_month_range(mpr_month: str) -> list[tuple[str, int]] | None:
    """'January to March 2026' → [('January 2026',1),('February 2026',2),
    ('March 2026',3)].  None if it isn't a range."""
    m = _RANGE_RE.search(mpr_month or "")
    if not m:
        return None
    start, end, year = m.group(1).title(), m.group(2).title(), m.group(3)
    if start not in _MONTH_LIST or end not in _MONTH_LIST:
        return None
    si, ei = _MONTH_LIST.index(start), _MONTH_LIST.index(end)
    if ei < si:
        return None
    return [(f"{_MONTH_LIST[i]} {year}", i + 1) for i in range(si, ei + 1)]


def _fmt_leaves(days: float) -> float | int:
    """Whole numbers as int (2), fractional kept as float (0.5)."""
    return int(days) if float(days).is_integer() else days


def _emp_leaves_for_month(
    certs: list[tuple[str, dict[int, float]]], emp_name: str, month_num: int
) -> float | int:
    """Look up an employee's leave days for a month by fuzzy name match."""
    etoks = _name_tokens(emp_name)
    for cert_name, by_month in certs:
        if etoks & _name_tokens(cert_name):
            return _fmt_leaves(by_month.get(month_num, 0.0))
    return 0


def _looks_like_name(cell: str, designation: str) -> bool:
    cs = cell.strip()
    if not cs or cs == designation:
        return False
    if _DESIG_HINTS.search(cs) or _DATE_RE.search(cs):
        return False
    if re.fullmatch(r"[\d\-\.\s/]+", cs):          # pure number / dash / date-ish
        return False
    # Reject header phrases: a cell whose every significant word is a header
    # word (e.g. "Date of Joining", "Name", "SI no", "Working Period").
    words = [w.lower().strip(".,()/:;-") for w in cs.split()]
    significant = [w for w in words if w and w not in _CONNECTORS]
    if significant and all(w in _HEADER_WORDS for w in significant):
        return False
    # A person name: contains a capitalised word — Title-case ("Rakesh") OR
    # ALL-CAPS ("RAKESH", common in deployment/designation tables, file18/19) —
    # and isn't absurdly long. (Header rows can't reach here as employees: they
    # carry no designation and are dropped by _employees_from_table.)
    return bool(re.search(r"[A-Z][a-zA-Z]", cs)) and len(cs.split()) <= 5


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

def _is_justification_table(rows: list[list[str]]) -> bool:
    """True for the 'Justification for Attendance not marked' / leave-reason
    grids that some MPRs append (columns: #, Date, Day, Reason). Those are NOT
    employee tables — their 'Day' cells (e.g. 'Friday') would otherwise be
    mistaken for names. We exclude them before picking a page's employee table.
    """
    head = " ".join(" ".join(r) for r in rows[:2]).lower()
    if "designation" in head:
        return False  # has a Designation column → it's the employee table
    return "justification" in head or ("reason" in head and "day" in head)


def _employees_from_table(rows: list[list[str]]) -> list[dict]:
    """Extract employee records from a parsed MPR table — CONTENT-driven.

    We do NOT rely on header-column indices, because NICSI MPR tables use a
    two-row header with colspan ("Working Period" spanning From/To), so header
    columns don't line up with data columns. Instead, per row we locate cells
    by what they contain:
      - designation = the cell matching designation hints
      - name        = a person-name-looking cell
      - leaves      = the LAST cell (the Absent column), parsed safely
    Header rows and the footer line have neither a designation nor a name, so
    they're skipped automatically.
    """
    employees: list[dict] = []
    for r in rows:
        joined = " ".join(r)
        if re.search(r"performance of the above|signature|grand total", joined, re.IGNORECASE):
            continue

        designation = next((c for c in r if _DESIG_HINTS.search(c)), "")
        name = next((c for c in r if _looks_like_name(c, designation)), "")

        # Designation fallback: not every MPR uses "Level N" titles — some use a
        # plain job title (e.g. "GIS DIGITIZATION SUPERVISOR") that matches no
        # hint. When there's a name but no hit, the cell right after the name is
        # the designation column. Guarded by the row containing a date (every
        # real employee row has a Date of Joining) so header rows — which have a
        # name-looking cell but no date — never get a fabricated designation.
        if name and not designation and _DATE_RE.search(joined):
            ni = r.index(name)
            cand = r[ni + 1].strip() if ni + 1 < len(r) else ""
            if (cand and len(cand) > 3 and not _DATE_RE.search(cand)
                    and not re.fullmatch(r"[\d\-\.\s/]+", cand)
                    and re.search(r"[A-Za-z]{2}", cand)):
                designation = cand

        # A real MPR employee row always resolves to a designation — either a
        # hint ("Level N" / "Software Application…") or, via the fallback above,
        # the job-title cell after the name. Header rows ("Sr No / Name /
        # Designation", "From / To", "AGENCY Name"), justification rows, and
        # other non-employee rows resolve to none, so requiring a designation
        # drops them. (A designation with no readable name is still kept — real
        # employee, OCR just missed the name.)
        if not designation:
            continue

        # Absent column is the rightmost cell.
        leaves = _leaves(r[-1]) if r else 0

        employees.append({
            "employee_name": " ".join(name.split()),
            "designation": _normalize_designation(designation),
            "leaves": leaves,
        })
    return employees


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_NUM_MONTH_RE = re.compile(r"\b(0?[1-9]|1[0-2])\s*/\s*(20\d{2})\b")
_BARE_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
    re.IGNORECASE,
)
_MONTH_ABBR = {m[:3].lower(): m for m in _MONTH_LIST}


def _norm_month_name(name: str) -> str:
    """'Apr'/'apr'/'APRIL' → 'April'."""
    n = name.strip().title()
    return _MONTH_ABBR.get(n[:3].lower(), n)


def _dominant_year(text: str) -> str:
    """The most-common 20xx year on the page (ties → the latest). '' if none."""
    years = re.findall(r"\b(20\d{2})\b", text)
    if not years:
        return ""
    from collections import Counter
    c = Counter(years)
    return max(c, key=lambda y: (c[y], int(y)))


def _find_month(page_text: str) -> str:
    """Return the MPR month string. Handles, in order of preference:
      - a month LABEL ("MPR for the Month", "MPR Month", "MPR for -", "for the month")
        then within its 60-char zone: a range ("January to March 2026"), a numeric
        "MM/YYYY" (NIC Digital), a "<Month> <Year>", or a bare "<Month>" whose year
        is taken from the page's dominant year (e.g. file8: "MPR Month: January").
      - no label → conservative "<Month> <Year>" anywhere on the page.
    Returns '' if nothing usable is found.
    """
    mm = (
        re.search(r"MPR\s+for\s+the\s+Month\s*[-:]*\s*", page_text, re.IGNORECASE)
        or re.search(r"MPR\s+Month\s*[-:]*\s*", page_text, re.IGNORECASE)
        or re.search(r"MPR\s+for\s*[-:]+\s*", page_text, re.IGNORECASE)
        or re.search(r"for\s+the\s+month\s+(?:of\s+)?", page_text, re.IGNORECASE)
    )
    if mm:
        zone = page_text[mm.end():mm.end() + 60]
        rng = _RANGE_RE.search(zone)
        if rng:
            return f"{_norm_month_name(rng.group(1))} to {_norm_month_name(rng.group(2))} {rng.group(3)}"
        num = _NUM_MONTH_RE.search(zone)
        if num:
            return f"{_MONTH_LIST[int(num.group(1)) - 1]} {num.group(2)}"
        mo = _MONTH_RE.search(zone)
        if mo:
            return f"{_norm_month_name(mo.group(1))} {mo.group(2)}"
        bm = _BARE_MONTH_RE.search(zone)
        if bm:
            yr = _dominant_year(page_text)
            return f"{_norm_month_name(bm.group(1))} {yr}".strip()
    # No usable label/zone — conservative fallback: a Month+Year anywhere.
    mo = _MONTH_RE.search(page_text)
    return f"{_norm_month_name(mo.group(1))} {mo.group(2)}" if mo else ""


def _merge_continuation_pages(base_months: list[dict]) -> list[dict]:
    """Collapse per-page records into one record per (work_order, month).

    MPR employee tables routinely span page breaks: page 1 holds employees 1-5,
    the next page holds 6+ but carries no month label of its own. And some MPRs
    place each employee on a separate page. Both produce several page-records for
    the same work order and month that must become a single record.

    Steps:
      1. A month-less record inherits the month of the most recent record for the
         same work order (it is a continuation page).
      2. Records sharing (work_order, month) merge — employees concatenated,
         de-duplicated by name (blank names are always kept; OCR missed them).
    Order is preserved (page order), so the first occurrence anchors the record.
    """
    last_month_by_wo: dict[str, str] = {}
    for m in base_months:
        if m["mpr_month"]:
            last_month_by_wo[m["work_order"]] = m["mpr_month"]
        elif m["work_order"] and m["work_order"] in last_month_by_wo:
            m["mpr_month"] = last_month_by_wo[m["work_order"]]

    merged: list[dict] = []
    index: dict[tuple, dict] = {}
    for m in base_months:
        key = (m["work_order"], m["mpr_month"])
        hit = index.get(key)
        if hit is None:
            index[key] = m
            merged.append(m)
            continue
        seen = {e["employee_name"].lower() for e in hit["employees"] if e["employee_name"]}
        for e in m["employees"]:
            nm = e["employee_name"].lower()
            if not nm or nm not in seen:
                hit["employees"].append(e)
                if nm:
                    seen.add(nm)
    return merged


def group_mpr(surya_result: dict[str, Any]) -> list[dict]:
    base_months: list[dict] = []          # pages that carry an employee table
    cert_texts: list[str] = []            # Leave Adjustment Certificate pages

    for page in surya_result.get("pages", []):
        blocks = page.get("blocks", [])
        page_text = " ".join(_strip_tags(b.get("html", "")) for b in blocks)

        # A certificate page contributes leave data, not its own month record.
        if re.search(r"leave\s+adjustment\s+certificate", page_text, re.IGNORECASE):
            cert_texts.append(page_text)
            continue

        wo = _WORK_ORDER_RE.search(page_text)
        work_order = wo.group(1) if wo else ""
        month = _find_month(page_text)

        tables = [
            _parse_table(b.get("html", ""))
            for b in blocks
            if (b.get("label") or "").lower() == "table" or "<table" in (b.get("html") or "")
        ]
        # Keep only real employee tables — drop the appended "Justification for
        # Attendance" grids so their day/reason rows don't leak as employees.
        tables = [t for t in tables if t and not _is_justification_table(t)]
        employees = _employees_from_table(max(tables, key=len)) if tables else []

        # Skip blank/continuation pages that have neither a month nor employees.
        if employees or month:
            base_months.append({
                "work_order": work_order,
                "mpr_month": month,
                "employees": employees,
            })

    base_months = _merge_continuation_pages(base_months)

    certs = [_parse_leave_certificate(t) for t in cert_texts]
    certs = [(n, bm) for (n, bm) in certs if n]

    # Expand any month range into one record per month. When certificates are
    # present, each employee's leaves are computed per month from the dates;
    # otherwise the split months carry 0 (no per-month data available).
    result: list[dict] = []
    for m in base_months:
        rng = _parse_month_range(m["mpr_month"])
        if rng:
            for month_name, month_num in rng:
                result.append({
                    "work_order": m["work_order"],
                    "mpr_month": month_name,
                    "employees": [
                        {
                            "employee_name": e["employee_name"],
                            "designation": e["designation"],
                            "leaves": _emp_leaves_for_month(certs, e["employee_name"], month_num)
                            if certs else 0,
                        }
                        for e in m["employees"]
                    ],
                })
        else:
            result.append(m)

    _reconcile_roster(result)
    return result


def _reconcile_roster(months: list[dict]) -> None:
    """Fill OCR gaps from the same work order's roster across months."""
    roster: dict[str, dict[int, dict[str, str]]] = {}
    for m in months:
        slot = roster.setdefault(m["work_order"], {})
        for i, e in enumerate(m["employees"]):
            cur = slot.setdefault(i, {"employee_name": "", "designation": ""})
            if e["employee_name"] and not cur["employee_name"]:
                cur["employee_name"] = e["employee_name"]
            if len(e["designation"]) > len(cur["designation"]):
                cur["designation"] = e["designation"]
    for m in months:
        slot = roster.get(m["work_order"], {})
        while len(m["employees"]) < len(slot):
            m["employees"].append({"employee_name": "", "designation": "", "leaves": 0})
        for i, e in enumerate(m["employees"]):
            ref = slot.get(i)
            if not ref:
                continue
            if not e["employee_name"] and ref["employee_name"]:
                e["employee_name"] = ref["employee_name"]
            if len(ref["designation"]) > len(e["designation"]):
                e["designation"] = ref["designation"]
