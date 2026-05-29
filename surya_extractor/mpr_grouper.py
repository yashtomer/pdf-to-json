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
    "si", "no", "sno", "sl", "name", "designation", "date", "joining",
    "working", "period", "from", "to", "absent", "leaves", "leave", "taken",
    "employee", "project", "work", "order", "mpr", "month", "performance",
    "report", "remarks",
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
    words = [w.lower().strip(".,") for w in cs.split()]
    significant = [w for w in words if w and w not in _CONNECTORS]
    if significant and all(w in _HEADER_WORDS for w in significant):
        return False
    # A person name: has a capitalised letter and isn't absurdly long
    return bool(re.search(r"[A-Z][a-z]", cs)) and len(cs.split()) <= 5


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

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

        if not (designation or name):
            continue  # header row / non-employee row

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

def group_mpr(surya_result: dict[str, Any]) -> list[dict]:
    months: list[dict] = []
    for page in surya_result.get("pages", []):
        blocks = page.get("blocks", [])
        page_text = " ".join(_strip_tags(b.get("html", "")) for b in blocks)

        wo = _WORK_ORDER_RE.search(page_text)
        work_order = wo.group(1) if wo else ""

        month = ""
        mm = re.search(r"MPR\s+for\s+the\s+Month\s*:?\s*", page_text, re.IGNORECASE)
        zone = page_text[mm.end():mm.end() + 40] if mm else page_text
        mo = _MONTH_RE.search(zone) or _MONTH_RE.search(page_text)
        if mo:
            month = f"{mo.group(1).title()} {mo.group(2)}"

        # Parse every table block; use the one with the most rows as the roster.
        tables = [
            _parse_table(b.get("html", ""))
            for b in blocks
            if (b.get("label") or "").lower() == "table" or "<table" in (b.get("html") or "")
        ]
        tables = [t for t in tables if t]
        employees: list[dict] = []
        if tables:
            best = max(tables, key=len)
            employees = _employees_from_table(best)

        months.append({
            "work_order": work_order,
            "mpr_month": month,
            "employees": employees,
        })

    _reconcile_roster(months)
    return months


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
