"""
Robust PDF reader that handles text-based PDFs, scanned PDFs, and signed/AcroForm PDFs.

USAGE FROM THE COMMAND LINE
---------------------------
    # Pass a PDF, get JSON on stdout:
    python pdf_reader.py invoice.pdf

    # Multiple PDFs (returns a JSON array):
    python pdf_reader.py file1.pdf file2.pdf

    # Pipe straight into jq:
    python pdf_reader.py invoice.pdf | jq '.pages[0].fields'

    # Write per-file JSON to a folder:
    python pdf_reader.py *.pdf --dump-json out/

    # Human-readable summary instead of JSON:
    python pdf_reader.py invoice.pdf --summary

USAGE FROM PYTHON
-----------------
    from pdf_reader import pdf_to_json
    data = pdf_to_json("invoice.pdf")   # → dict, ready for json.dumps()

Strategy:
  1. Try pdfplumber for text extraction (layout-aware, best for native text PDFs).
  2. Try pdfplumber for table extraction — each table becomes a list of dicts
     keyed by the (slugified, snake_case) column headers in row 0.
  3. If a page yields fewer than MIN_CHARS_PER_PAGE characters, treat it as
     image/scanned and fall back to OCR via pdf2image + pytesseract.
  4. All errors are caught per-page so one bad page can't kill the whole job.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pdfplumber

MIN_CHARS_PER_PAGE = 30  # below this, assume the page is scanned and needs OCR


def _clean_cell(cell: Optional[str]) -> str:
    """Collapse within-cell line breaks (PDF table wrapping) into a clean string.

    Heuristics for rejoining wrapped fragments (all other breaks become spaces):
      - "Person"\\n"s"      → "Persons"      (single trailing lowercase letter)
      - "14,09,808.9"\\n"8" → "14,09,808.98" (single trailing digit after a number)
      - "2,53,765"\\n".62"  → "2,53,765.62"  (decimal/comma continuation of a number)
    """
    if cell is None:
        return ""
    parts = [p.strip() for p in cell.replace("\r", "\n").split("\n") if p.strip()]
    if not parts:
        return ""
    out = parts[0]
    for p in parts[1:]:
        last = out[-1:]
        if len(p) == 1 and p.islower() and last.isalpha():
            out += p
        elif len(p) == 1 and p.isdigit() and last.isdigit():
            out += p
        elif p[:1] in (".", ",") and last.isdigit() and all(c.isdigit() or c in ".," for c in p):
            out += p
        else:
            out += " " + p
    return " ".join(out.split())


def _clean_table(table: list[list[Optional[str]]]) -> list[list[str]]:
    return [[_clean_cell(c) for c in row] for row in table]


def _slugify(s: str) -> str:
    """Convert a header string into a lowercase snake_case identifier.

    Lowercase, % → pct, all other non-alphanumeric runs → "_", trim leading/trailing "_".
    Examples:
      "Description"               → "description"
      "S. No"                     → "s_no"
      "HSN/ SAC Code"             → "hsn_sac_code"
      "Required Period (Months)"  → "required_period_months"
      "CGST (%)"                  → "cgst_pct"
    """
    if not s:
        return ""
    s = s.replace("%", "pct")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s)
    return s.strip("_").lower()


def _make_unique_headers(row: list[str]) -> list[str]:
    """Convert a header row into safe, unique snake_case dict keys."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for i, h in enumerate(row):
        key = _slugify(h or "") or f"col_{i + 1}"
        if key in seen:
            seen[key] += 1
            key = f"{key}_{seen[key]}"
        else:
            seen[key] = 1
        out.append(key)
    return out


def _table_to_keyed(table: list[list[str]]) -> list[dict[str, str]]:
    """Turn a [[header...], [row...], ...] table into [{header: cell}, ...].

    Skips column-letter annotation rows like "(A) (B) (C) ..." (sub-headers
    that reference formula columns, e.g. (AxBxC) — not actual data).
    """
    if not table or len(table) < 2:
        return []
    headers = _make_unique_headers(table[0])
    keyed: list[dict[str, str]] = []
    for row in table[1:]:
        d: dict[str, str] = {}
        for i, cell in enumerate(row):
            key = headers[i] if i < len(headers) else f"col_{i + 1}"
            d[key] = cell
        if _is_column_letter_row(d):
            continue
        keyed.append(d)
    return keyed


_KNOWN_BARE_LABELS = {"date", "pi number", "s. no", "s.no", "page", "ref no"}


def _is_label(cell: str) -> bool:
    if not cell:
        return False
    c = cell.strip()
    if c.endswith((":", ":-")):
        return True
    return c.lower() in _KNOWN_BARE_LABELS


def _strip_label(cell: str) -> str:
    return cell.strip().rstrip(":-").strip()


def _is_kv_table(table: list[list[str]]) -> bool:
    """A table is "key-value" (a document header box) when ≥20% of non-empty cells are labels."""
    if not table or len(table) < 2:
        return False
    label_count = sum(1 for row in table for cell in row if _is_label(cell))
    total = sum(1 for row in table for cell in row if cell and cell.strip())
    return total > 0 and label_count / total >= 0.20


def _kv_table_to_dict(table: list[list[str]]) -> dict[str, str]:
    """Flatten a key-value-style table into {slugified_label: value}."""
    fields: dict[str, str] = {}
    for row in table:
        i = 0
        while i < len(row):
            cell = row[i] if i < len(row) else ""
            if _is_label(cell):
                key = _slugify(_strip_label(cell))
                value = ""
                for j in range(i + 1, len(row)):
                    nc = (row[j] or "").strip()
                    if not nc:
                        continue
                    if _is_label(nc):
                        break
                    value = nc
                    i = j
                    break
                if key and (key not in fields or not fields[key]):
                    fields[key] = value
            i += 1
    return fields


_TEXT_FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("subject", re.compile(r"Subject\s*:\s*(.+?)(?:\n\s*\n|\n\s*Sir\b|$)", re.DOTALL)),
    ("work_order_no", re.compile(r"(?:Work\s+Order|Order\.)\s*No\.?\s*:-?\s*([A-Z]\d+)", re.IGNORECASE)),
    ("project_no", re.compile(r"Project No\.?\s*:-?\s*(\S+)")),
    ("po_no", re.compile(r"PO No\.?\s*:-?\s*(\S+)")),
    ("empanelment_no", re.compile(r"Empanelment No\s*:\s*(\S+)")),
    ("valid_till", re.compile(r"Valid Till\s*:\s*(\S+)")),
    ("gstin", re.compile(r"GSTIN(?:\s*No\.?)?(?:\s*of\s*\w+)?\s*:\s*([A-Z0-9]+)", re.IGNORECASE)),
    ("mpr_for_month", re.compile(r"MPR\s+for\s+the\s+Month\s*:\s*(.+?)(?:\s+(?:Work|Project)\b|\n|$)", re.IGNORECASE)),
    ("report_for_month", re.compile(r"for\s+the\s+month\s+([A-Z][A-Za-z]+\s+\d{4})", re.IGNORECASE)),
    ("vendor_name", re.compile(r"Vendor\s+Name\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)),
    ("document_type", re.compile(
        # Service Performance Report variants — including Adobe Scan OCR's
        # tendency to glue the leading "S" of "Service" onto "Monthly" and
        # leave "ervice" behind ("MonthlyS ervice Performance Report").
        r"\b(Monthly\s*[Ss]?\s*[Ss]?ervice\s+Performance\s+Report"
        r"|Service\s+Performance\s+Report"
        r"|Monthly\s+Performance\s+Report"
        r"|Work\s+Order|Purchase\s+Order|Invoice)\b",
        re.IGNORECASE,
    )),
]


def _normalize_doc_type(raw: str) -> str:
    """Collapse OCR variants of doc-type strings to a canonical name."""
    s = " ".join(raw.split())
    s_lower = s.lower()
    if "service performance report" in s_lower or "ervice performance report" in s_lower:
        return "Monthly Service Performance Report" if "monthly" in s_lower else "Service Performance Report"
    if "monthly performance report" in s_lower:
        return "Monthly Performance Report"
    return s


_SIGNATURE_BLOCK_RE = re.compile(
    r"""For\s+(?P<authority>[^\n]+?)\s*\n
        (?:\s*Digitally\s+signed\s+by\s+(?P<signed_by>[^\n]+?)\s*\n
           \s*Date:\s*(?P<signed_on>[^\n]+?)\s*\n)?
        \s*\(\s*(?P<signatory_name>[^)\n]+?)\s*\)\s*\n
        (?P<designation>(?:[^\n]+\n){1,3}?)
        (?=\s*Copy\s+To:|\s*Page\s+\d+\s+of|\Z)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_COPY_TO_RE = re.compile(
    r"Copy\s+To:\s*\n(?P<block>.+?)(?:\n\s*Page\s+\d+\s+of|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_COPY_TO_ITEM_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s*$")


def _extract_signature_block(text: str) -> dict[str, str]:
    """Extract issuing_authority / signed_by / signed_on / signatory_name / signatory_designation.

    Handles both formats:
      - With digital signature: "For X\nDigitally signed by Y\nDate: Z\n(name)\ndesignation"
      - Printed only:           "For X\n(name)\ndesignation"
    """
    m = _SIGNATURE_BLOCK_RE.search(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    if m.group("authority"):
        out["issuing_authority"] = " ".join(m.group("authority").split())
    if m.group("signed_by"):
        out["signed_by"] = " ".join(m.group("signed_by").split())
    if m.group("signed_on"):
        out["signed_on"] = " ".join(m.group("signed_on").split())
    out["signatory_name"] = " ".join(m.group("signatory_name").split())
    desig = " ".join(m.group("designation").split()).rstrip(" &").strip()
    if desig:
        out["signatory_designation"] = desig
    return out


def _extract_copy_to(text: str) -> Optional[list[str]]:
    """Extract the 'Copy To:' numbered list as a list of strings."""
    m = _COPY_TO_RE.search(text)
    if not m:
        return None
    items: list[str] = []
    for line in m.group("block").split("\n"):
        line_m = _COPY_TO_ITEM_RE.match(line)
        if line_m:
            items.append(line_m.group(1).strip())
    return items or None


def _extract_text_fields(text: str) -> dict[str, str | list[str]]:
    """Pull labelled lines, signature block, and Copy To list out of free-form text."""
    fields: dict[str, str | list[str]] = {}
    for key, pattern in _TEXT_FIELD_PATTERNS:
        m = pattern.search(text)
        if m:
            value = " ".join(m.group(1).split()).rstrip(",.;")
            if key == "document_type":
                value = _normalize_doc_type(value)
            fields[key] = value
    fields.update(_extract_signature_block(text))
    copy_to = _extract_copy_to(text)
    if copy_to:
        fields["copy_to"] = copy_to
    return fields


_COLUMN_LETTER_RE = re.compile(r"^\([A-Z]\)$")


def _is_column_letter_row(row: dict[str, str]) -> bool:
    """A row like {"col_x": "(A)", "col_y": "(B)", ...} — formula labels, not data."""
    non_empty = [v.strip() for v in row.values() if v and v.strip()]
    if not non_empty:
        return False
    matches = sum(1 for v in non_empty if _COLUMN_LETTER_RE.match(v))
    return matches / len(non_empty) >= 0.7


_TOTAL_ROW_HINTS = ("total", "grand total", "subtotal", "sub-total", "amount in")


def _is_total_row(row: dict[str, str]) -> bool:
    """A row whose first non-empty cell is a totals label."""
    for v in row.values():
        if v and v.strip():
            s = v.strip().lower()
            return any(h in s for h in _TOTAL_ROW_HINTS)
    return False


_TAX_HEADER_RE = re.compile(r"^(.*?)_pct_amount$", re.IGNORECASE)
_TAX_VALUE_RE = re.compile(r"^\s*([\d.,]+\s*%)\s*(.*)$")


def _split_tax_header(header: str) -> Optional[tuple[str, str]]:
    """Split a slugified combined-tax header (e.g. "cgst_pct_amount") into rate + amount keys."""
    m = _TAX_HEADER_RE.match(header)
    if not m:
        return None
    prefix = m.group(1).strip("_")
    if not prefix:
        return None
    return (f"{prefix}_pct", f"{prefix}_amount")


def _split_tax_value(value: str) -> tuple[str, str]:
    """Split "18.00% 56,844.80" → ("18.00%", "56,844.80"). Missing parts return ""."""
    if not value:
        return "", ""
    m = _TAX_VALUE_RE.match(value)
    if m:
        return m.group(1).replace(" ", ""), m.group(2).strip()
    return "", value.strip()


def _split_combined_tax_cells(row: dict[str, str]) -> dict[str, str]:
    """Rewrite a row dict so combined "X (%) /Amount" cells become two separate fields."""
    new_row: dict[str, str] = {}
    for key, value in row.items():
        split_keys = _split_tax_header(key)
        if split_keys:
            rate_key, amt_key = split_keys
            rate, amt = _split_tax_value(value)
            new_row[rate_key] = rate
            new_row[amt_key] = amt
        else:
            new_row[key] = value
    return new_row


@dataclass
class PageResult:
    page_number: int
    text: str
    method: str                                  # "text" | "ocr" | "empty"
    char_count: int
    fields: dict[str, str | list[str]] = field(default_factory=dict)
    tables: list[list[dict[str, str]]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class DocumentResult:
    file: str
    page_count: int
    pages: list[PageResult] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)


def _ocr_page(pdf_path: Path, page_number: int, dpi: int = 300) -> str:
    from pdf2image import convert_from_path
    import pytesseract

    images = convert_from_path(
        str(pdf_path),
        dpi=dpi,
        first_page=page_number,
        last_page=page_number,
    )
    if not images:
        return ""
    return pytesseract.image_to_string(images[0])


# ---------------------------------------------------------------------------
# NICSI MPR (Monthly Performance Report) — format-specific OCR-text parser
# ---------------------------------------------------------------------------
# Two layouts seen in the wild:
#   Layout A — Group MPR: one designation, multiple numbered team members
#              (e.g. "1. A.Siva Naga Prasad", "2. Gauraw Shrivastava")
#   Layout B — Multi-row MPR: each employee on their own line starting with
#              "<si_no> | <name> | ... <date> | <date> | <date>"
#
# Pure-regex extraction works better than bbox clustering for this format
# because OCR badly interleaves the visual columns into the text stream.

_MPR_DATE_HYPHEN = re.compile(r"\d{1,2}-[A-Z][a-z]{2}-\d{4}")
_MPR_DATE_SLASH = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
_MPR_DATE_ANY = re.compile(r"\d{1,2}[-/](?:[A-Z][a-z]{2}|\d{1,2})[-/]\d{2,4}")

# Words that show up in designations — used to filter them OUT when guessing
# employee names from the OCR text. Lowercase.
_DESIGNATION_WORDS = {
    "level", "minimum", "work", "experience", "experience.", "years", "year",
    "with", "one", "two", "1st", "2nd", "3rd", "4th",
    "increment", "increment-tier", "tier", "tier-",
    "software", "application", "support", "engineer",
    "relevant", "to", "less", "than", "or", "and",
}


def _rejoin_split_dates(text: str) -> str:
    """OCR sometimes splits "01-Apr-\\n2026" across lines. Stitch them back."""
    text = re.sub(r"(\d{1,2}-[A-Z][a-z]{2}-)\s*\n?\s*(\d{4})", r"\1\2", text)
    text = re.sub(r"(\d{1,2}/\d{1,2}/)\s*\n?\s*(\d{4})", r"\1\2", text)
    return text


def _extract_mpr_designation(text: str) -> str:
    """Build a designation from atomic tokens (avoids grabbing interleaved column content).

    Looks for known fragments anywhere in the text and stitches them together,
    so OCR's interleaving of vertical column content can't pollute the result.
    """
    parts: list[str] = []
    if m := re.search(r"\bLevel\s+\d+\b", text):
        parts.append(m.group(0).strip())
    elif re.search(r"Software\s+Application\s+Support\s+Engineer", text):
        parts.append("Software Application Support Engineer")

    # Experience phrase. Try several patterns:
    #   "experience N years" — Adobe Scan style (file4: "experience 1 years)")
    #   "work experience N"  — VersaLink OCR style (yatendra: "work experience 1 ...")
    #   "(N to less than M years ...)" — NICSI Software Engineer style
    if m := re.search(r"experience\s+(\d+)\s+years?", text, re.IGNORECASE):
        parts.append(f"(Minimum work experience {m.group(1)} years)")
    elif m := re.search(r"work\s+experience\s+(\d+)", text, re.IGNORECASE):
        parts.append(f"(Minimum work experience {m.group(1)} years)")
    elif m := re.search(r"(\d+)\s+to\s+less\s+than\s+(\d+)\s+years?", text, re.IGNORECASE):
        parts.append(f"({m.group(1)} to less than {m.group(2)} years relevant experience)")

    # "with <N|word> Increment" — Increment may be on a different line with
    # column content in between. Allow digit, ordinal, or words "one|two|three".
    m_with = re.search(r"with\s+(\d+(?:st|nd|rd|th)?|one|two|three)\b", text, re.IGNORECASE)
    has_inc = re.search(r"\b[Ii]ncrement\b", text)
    if m_with and has_inc:
        parts.append(f"with {m_with.group(1).lower()} Increment")
    elif m := re.search(r"(\d+(?:st|nd|rd|th))\s+year\s+(\d+(?:st|nd|rd|th|[\"”]))\s+[Ii]ncrement", text):
        parts.append(f"{m.group(1)} year {m.group(2)} Increment")

    # "Tier - N" or "Tier N" or "Tier-N" (separator optional)
    if m := re.search(r"Tier\s*-?\s*(\d+)", text):
        parts.append(f"— Tier - {m.group(1)}")
    return " ".join(parts)


def _extract_mpr_period_dates(text: str) -> tuple[str, str]:
    """Find "Working Period From / To" dates, including OCR-split forms like
    "01-Apr- 30-Apr-\\n2026 2026" (stems on one line, years on another).
    """
    # First try a complete-date pair on adjacent positions
    cleaned = _rejoin_split_dates(text)
    pair_re = re.compile(
        r"(\d{1,2}-[A-Z][a-z]{2}-\d{4})\s+(?:To\s+)?(\d{1,2}-[A-Z][a-z]{2}-\d{4})"
    )
    pair_slash = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{4})\s+(?:To\s+)?(\d{1,2}/\d{1,2}/\d{4})"
    )
    if m := pair_re.search(cleaned):
        return (m.group(1), m.group(2))
    if m := pair_slash.search(cleaned):
        return (m.group(1), m.group(2))

    # Stems on one line + years on next line pattern:
    # "01-Apr- 30-Apr- <other text>\n<other text> 2026 2026"
    # `(?<!\d)\d{4}(?!\d)` ensures we don't accidentally match "2" from "2nd Increment".
    split_pair = re.compile(
        r"(\d{1,2}-[A-Z][a-z]{2}-)\s+(\d{1,2}-[A-Z][a-z]{2}-)"
        r".*?(?<!\d)(\d{4})(?!\d)\s+(?<!\d)(\d{4})(?!\d)",
        re.DOTALL,
    )
    if m := split_pair.search(text):
        return (m.group(1) + m.group(3), m.group(2) + m.group(4))
    return ("", "")


# ---------------------------------------------------------------------------
# NICSI SPR (Service Performance Report) — format-specific parser
# ---------------------------------------------------------------------------
# SPR rows have the shape:
#   <designation prefix>  <date_from>  <date_to>  <candidate>  <PM>  <performance>
# All five tail fields land on one line in pdfplumber's extracted text, e.g.:
#   "(Minimum work 01-01-26 31-01-26 Aravind JB Unnikrishnan B Very Good"
# The designation, qty, and S.No appear on surrounding lines.

_SPR_PERFORMANCE_RE = re.compile(
    r"\b(Very\s+Good|Excellent|Satisfactory|Unsatisfactory|Average|Good|Poor)\b",
    re.IGNORECASE,
)

_SPR_ROW_RE = re.compile(
    r"(\d{1,2}-\d{1,2}-\d{2,4})\s+"          # from date
    r"(\d{1,2}-\d{1,2}-\d{2,4})\s+"          # to date
    r"(?P<rest>.+?)\s+"                       # candidate + PM
    r"(?P<perf>Very\s+Good|Excellent|Satisfactory|Unsatisfactory|Average|Good|Poor)"
    r"\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _split_candidate_pm(rest: str) -> tuple[str, str]:
    """Split "Aravind JB Unnikrishnan B" into ("Aravind JB", "Unnikrishnan B").

    NICSI Project Manager names typically end in a single-letter initial
    preceded by a multi-letter surname (e.g. "Unnikrishnan B"). When that
    pattern matches at the end, take the last two words as the PM. Otherwise
    fall back to splitting the words roughly in half.
    """
    words = rest.split()
    if len(words) < 2:
        return (rest.strip(), "")
    # Preferred: <multi-letter surname> <single-letter initial> at the end
    if len(words[-1]) == 1 and len(words[-2]) > 1 and words[-2][0].isupper():
        return (" ".join(words[:-2]), " ".join(words[-2:]))
    # Fallback: split in half
    mid = (len(words) + 1) // 2
    return (" ".join(words[:mid]), " ".join(words[mid:]))


def _extract_spr_table(text: str) -> list[dict[str, str]]:
    """Extract a NICSI Service Performance Report table from extracted text."""
    designation = _extract_mpr_designation(text)
    rows: list[dict[str, str]] = []
    for i, m in enumerate(_SPR_ROW_RE.finditer(text), start=1):
        candidate, pm = _split_candidate_pm(m.group("rest"))
        rows.append({
            "s_no": str(i),
            "designation": designation,
            "qty": "1",
            "service_period_from": m.group(1),
            "service_period_to": m.group(2),
            "candidate_name": candidate,
            "project_manager_name": pm,
            "overall_performance": " ".join(m.group("perf").split()),
        })
    return rows


def _extract_mpr_table(text: str) -> list[dict[str, str | list[str]]]:
    """Extract a NICSI MPR table from OCR text. Handles both group and multi-row layouts."""
    cleaned = _rejoin_split_dates(text)

    # Pre-count date triplets so we can sanity-check Layout B's results.
    triplet_re = re.compile(
        r"(\d{1,2}[-/](?:[A-Z][a-z]{2}|\d{1,2})[-/]\d{2,4})\s+"
        r"(\d{1,2}[-/](?:[A-Z][a-z]{2}|\d{1,2})[-/]\d{2,4})\s+"
        r"(\d{1,2}[-/](?:[A-Z][a-z]{2}|\d{1,2})[-/]\d{2,4})"
        r"(?:\s+(\d+))?",
    )
    triplet_count = len(triplet_re.findall(cleaned))

    # Layout B: lines like "<si_no> [|] <Name…> ... <date> [|] <date> [|] <date>"
    # Both `|` separators (after si_no AND between dates) may be missing in OCR.
    # Name grows until just before "(" or a digit (so multi-word names like
    # "Ch. Kiran" / "K Vijay" stay whole). Years can be 2 or 4 digits.
    rows_b: list[dict[str, str | list[str]]] = []
    for m in re.finditer(
        r"^\s*(\d+)\s*\|?\s*([A-Z][\w.'\-\s]+?)\s*(?=[\(\d])"
        r".*?"
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\|?\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*\|?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
        cleaned,
        re.MULTILINE,
    ):
        rows_b.append({
            "si_no": m.group(1).strip(),
            "employee_name": m.group(2).strip().rstrip(".,; "),
            "designation": _extract_mpr_designation(cleaned),
            "date_of_joining": m.group(3),
            "working_period_from": m.group(4),
            "working_period_to": m.group(5),
        })
    # Only trust Layout B if it found at least as many rows as date-triplets in the text
    if rows_b and len(rows_b) >= triplet_count:
        return rows_b

    # Layout C: split text by designation-block boundaries (each row starts
    # with a new "Level X" / "Software Application..." marker). Works for
    # PDFs where pdfplumber re-orders columns and the SI numbers don't align
    # with the name lines (e.g. Adobe Scan output of file4-style MPRs).
    boundary_re = re.compile(
        r"\b(?:Level\s+\d+|Software\s+Application\s+Support\s+Engineer)\b"
    )
    boundaries = [m.start() for m in boundary_re.finditer(cleaned)]
    if len(boundaries) >= 1:
        rows_c: list[dict[str, str | list[str]]] = []
        header_words = {
            "monthly", "performance", "report", "project", "no", "mpr", "month",
            "work", "order", "name", "designation", "date", "of", "joining",
            "working", "period", "from", "to", "absent", "service", "details",
            "candidate", "manager", "leave", "remarks", "satisfactory",
            "overall", "ref", "vendor", "sir", "madam", "dated", "scientist",
            "national", "informatics", "centre", "signature", "stamp",
            "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
            "sep", "oct", "nov", "dec",
        }
        footer_re = re.compile(
            r"\bPerformance\s+of\s+the\s+above\b"
            r"|\bSignature\s*[&(]"
            r"|\bDated:\s*\d"
            r"|\b(?:NATIONAL\s+INFORMATICS|National\s+Informatics)\b",
        )
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(cleaned)
            chunk = cleaned[start:end]
            # Trim away anything past the table's footer / signature block
            if fm := footer_re.search(chunk):
                chunk = chunk[: fm.start()]
            # Need at least 3 dates to form a row
            chunk_triplet = triplet_re.search(chunk)
            if not chunk_triplet:
                continue
            # SI no: digit on its own line/word; fall back to position
            si_m = re.search(r"(?:^|\n)\s*(\d+)\s*(?:\||\n|\s)", chunk)
            si_no = si_m.group(1) if si_m else str(i + 1)
            # Name: capitalized non-designation, non-header words. Single-letter
            # words like "M" are kept as middle initials but obvious column
            # labels ("S", "I", "A") are filtered.
            name_words: list[str] = []
            single_letter_skip = {"s", "i", "a"}
            for w in re.findall(r"\b([A-Z](?:[a-zA-Z.']*))\b", chunk):
                lw = w.lower().rstrip(".,;")
                if lw in _DESIGNATION_WORDS or lw in header_words:
                    continue
                if len(lw) == 1 and lw in single_letter_skip:
                    continue
                name_words.append(w)
            employee_name = " ".join(name_words)
            designation = _extract_mpr_designation(chunk)
            row: dict[str, str | list[str]] = {
                "si_no": si_no,
                "employee_name": employee_name,
                "designation": designation,
                "date_of_joining": chunk_triplet.group(1),
                "working_period_from": chunk_triplet.group(2),
                "working_period_to": chunk_triplet.group(3),
            }
            if chunk_triplet.group(4):
                row["absent"] = chunk_triplet.group(4)
            rows_c.append(row)
        if rows_c:
            return rows_c

    # Layout A: group format — one designation, multiple team members.
    # Numbered names can appear anywhere on a line (NICSI OCR interleaves
    # column content), but they reliably end at the line break.
    members: list[str] = []
    seen_nums: set[str] = set()
    for m in re.finditer(
        r"(\d+)[\.,]\s+([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+)*)\s*(?=\n|$)",
        text,
        re.MULTILINE,
    ):
        num = m.group(1).strip()
        name = m.group(2).strip()
        if num in seen_nums:
            continue
        seen_nums.add(num)
        members.append(f"{num}. {name}")

    all_dates = _MPR_DATE_HYPHEN.findall(cleaned)
    designation = _extract_mpr_designation(text)
    period_from, period_to = _extract_mpr_period_dates(text)

    # Date of joining = first complete date that isn't already used as a period date
    used = {period_from, period_to}
    date_of_joining = next((d for d in all_dates if d not in used), "")

    if not (members or designation or all_dates):
        return []

    return [{
        "si_no": "1",
        "designation": designation,
        "date_of_joining": date_of_joining,
        "working_period_from": period_from,
        "working_period_to": period_to,
        "team_members": members,
    }]


def read_pdf(
    path: str | Path,
    ocr_fallback: bool = True,
    ocr_dpi: int = 400,
    password: str = "",
    split_tax_cells: bool = False,
) -> DocumentResult:
    path = Path(path)
    result = DocumentResult(file=str(path), page_count=0)

    if not path.exists():
        result.error = f"File not found: {path}"
        return result

    try:
        with pdfplumber.open(path, password=password) as pdf:
            result.page_count = len(pdf.pages)
            result.metadata = {k: str(v) for k, v in (pdf.metadata or {}).items()}

            for idx, page in enumerate(pdf.pages, start=1):
                page_result = PageResult(
                    page_number=idx, text="", method="empty", char_count=0
                )
                try:
                    text = page.extract_text() or ""
                except Exception as e:
                    text = ""
                    page_result.error = f"text-extract: {e!r}"

                try:
                    raw_tables = page.extract_tables() or []
                    cleaned = [_clean_table(t) for t in raw_tables if t]
                    data_tables: list[list[list[str]]] = []
                    for t in cleaned:
                        if _is_kv_table(t):
                            page_result.fields.update(_kv_table_to_dict(t))
                        else:
                            data_tables.append(t)
                    page_result.tables = [_table_to_keyed(t) for t in data_tables]
                    if split_tax_cells:
                        page_result.tables = [
                            [_split_combined_tax_cells(row) for row in table]
                            for table in page_result.tables
                        ]
                except Exception as e:
                    page_result.error = (
                        (page_result.error + " | " if page_result.error else "")
                        + f"table-extract: {e!r}"
                    )

                if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                    page_result.text = text
                    page_result.method = "text"
                elif ocr_fallback:
                    try:
                        ocr_text = _ocr_page(path, idx, dpi=ocr_dpi)
                        page_result.text = ocr_text
                        page_result.method = "ocr" if ocr_text.strip() else "empty"
                    except Exception as e:
                        page_result.error = (
                            (page_result.error + " | " if page_result.error else "")
                            + f"ocr: {e!r}"
                        )

                if page_result.text:
                    for k, v in _extract_text_fields(page_result.text).items():
                        page_result.fields.setdefault(k, v)

                # Format-specific table extraction for NICSI MPR / SPR documents.
                # Runs on both text-based and OCR'd PDFs when pdfplumber's generic
                # table detection failed to find anything.
                if not page_result.tables:
                    doc_type = (page_result.fields.get("document_type") or "").lower()
                    if "service performance report" in doc_type:
                        spr_rows = _extract_spr_table(page_result.text)
                        if spr_rows:
                            page_result.tables.append(spr_rows)
                    elif "monthly performance report" in doc_type:
                        mpr_rows = _extract_mpr_table(page_result.text)
                        if mpr_rows:
                            page_result.tables.append(mpr_rows)

                page_result.char_count = len(page_result.text)
                result.pages.append(page_result)

    except Exception as e:
        result.error = f"open: {e!r}"

    return result


def pdf_to_json(
    path: str | Path,
    *,
    split_tax_cells: bool = True,
    include_text: bool = False,
    ocr_dpi: int = 400,
    password: str = "",
    as_string: bool = False,
) -> dict | str:
    """Read a PDF file and return its structured contents as JSON-ready data.

    This is the high-level convenience function for use in other projects.
    For more control (per-page iteration, custom error handling), use
    ``read_pdf()`` and work with the returned ``DocumentResult`` directly.

    Args:
        path: Path to the PDF file.
        split_tax_cells: Split combined "X (%) /Amount" cells into separate
            "<x>_pct" and "<x>_amount" keys. Default True.
        include_text: Include the bulky raw per-page ``text`` field in the
            output. Default False — most callers want the structured
            ``fields`` and ``tables`` instead.
        ocr_dpi: DPI for rendering scanned pages before OCR. Default 400.
        password: Password for encrypted PDFs.
        as_string: Return a pretty-printed JSON string instead of a dict.
            Default False — returns a dict you can manipulate or pass to
            ``json.dumps``.

    Returns:
        A dict (or JSON string when ``as_string=True``) shaped like::

            {
              "file": "...",
              "page_count": N,
              "metadata": {...},
              "pages": [
                {
                  "page_number": 1,
                  "method": "text",          # "text" | "ocr" | "empty"
                  "char_count": 5375,
                  "fields": {...},           # structured key/value pairs
                  "tables": [...],           # list of list-of-row-dicts
                  "error": null,
                },
                ...
              ],
              "error": null,
            }

    Example::

        from pdf_reader import pdf_to_json

        data = pdf_to_json("invoice.pdf")
        print(data["pages"][0]["fields"]["work_order_no"])

        # Or as a JSON string ready to write to a file or send over HTTP:
        json_str = pdf_to_json("invoice.pdf", as_string=True)
    """
    result = read_pdf(
        path,
        ocr_fallback=True,
        ocr_dpi=ocr_dpi,
        password=password,
        split_tax_cells=split_tax_cells,
    )
    data = asdict(result)
    if not include_text:
        for p in data.get("pages", []):
            p.pop("text", None)
    if as_string:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return data


__all__ = ["pdf_to_json", "read_pdf", "DocumentResult", "PageResult"]


def _print_human(doc: DocumentResult) -> None:
    print(f"=== {doc.file} ===")
    if doc.error:
        print(f"  ERROR: {doc.error}")
        return
    print(f"  Pages: {doc.page_count}")
    if doc.metadata:
        title = doc.metadata.get("Title") or doc.metadata.get("/Title") or ""
        producer = doc.metadata.get("Producer") or doc.metadata.get("/Producer") or ""
        if title:
            print(f"  Title: {title}")
        if producer:
            print(f"  Producer: {producer}")
    for p in doc.pages:
        marker = "OK" if p.text and not p.error else ("WARN" if p.text else "FAIL")
        suffix = f" ({p.error})" if p.error else ""
        table_rows = sum(len(t) for t in p.tables)
        table_info = f", {len(p.tables)} tables / {table_rows} rows" if p.tables else ""
        print(
            f"  [Page {p.page_number}] {marker} via {p.method}, "
            f"{p.char_count} chars{table_info}{suffix}"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read one or more PDFs and print structured JSON. "
                    "Pass a PDF path, get JSON on stdout."
    )
    parser.add_argument("paths", nargs="+", help="PDF file path(s)")
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback")
    parser.add_argument("--dpi", type=int, default=400, help="OCR DPI (default 400)")
    parser.add_argument("--password", default="", help="Password for encrypted PDFs")
    parser.add_argument(
        "--no-split-tax-cells",
        action="store_true",
        help='Keep combined "X (%%) /Amount" cells as a single field '
             "(by default the CLI splits them into <x>_pct and <x>_amount)",
    )
    parser.add_argument(
        "--include-text",
        action="store_true",
        help="Include the raw per-page 'text' field in JSON output (excluded by default)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a human-readable summary instead of JSON",
    )
    parser.add_argument(
        "--dump-text",
        metavar="DIR",
        help="Also write full extracted text per file into DIR/<stem>.txt",
    )
    parser.add_argument(
        "--dump-json",
        metavar="DIR",
        help="Also write a per-file structured JSON document into DIR/<stem>.json",
    )
    args = parser.parse_args(argv)

    results = [
        read_pdf(
            p,
            ocr_fallback=not args.no_ocr,
            ocr_dpi=args.dpi,
            password=args.password,
            split_tax_cells=not args.no_split_tax_cells,
        )
        for p in args.paths
    ]

    def _to_json(r: DocumentResult) -> dict:
        d = asdict(r)
        if not args.include_text:
            for p in d.get("pages", []):
                p.pop("text", None)
        return d

    if args.summary:
        for r in results:
            _print_human(r)
    else:
        json_results = [_to_json(r) for r in results]
        # Single PDF → one object; multiple PDFs → an array
        payload = json_results[0] if len(json_results) == 1 else json_results
        print(json.dumps(payload, indent=2, ensure_ascii=False))

    if args.dump_text:
        out_dir = Path(args.dump_text)
        out_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            if r.error:
                continue
            (out_dir / f"{Path(r.file).stem}.txt").write_text(r.full_text, encoding="utf-8")

    if args.dump_json:
        out_dir = Path(args.dump_json)
        out_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            (out_dir / f"{Path(r.file).stem}.json").write_text(
                json.dumps(_to_json(r), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    return 0 if all(not r.error for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
