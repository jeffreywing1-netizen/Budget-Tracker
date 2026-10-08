import io
import re
from typing import Optional

import pdfplumber

from .csv_generic import parse_amount, parse_date

_DATE = r"(\d{1,2}/\d{1,2}/\d{2,4})"
_MONEY = r"(\(?-?\$?[\d,]+\.\d{2}\)?)"
_AS_OF = rf"(?:\s+as\s+of\s+{_DATE})?"

PREVIOUS_RE = re.compile(rf"(?:Previous|Beginning|Opening)\s+Balance{_AS_OF}\s*:?\s*{_MONEY}", re.I)
NEW_RE = re.compile(rf"(?:New|Ending|Closing)\s+Balance{_AS_OF}\s*:?\s*{_MONEY}", re.I)
# "Billing Period: 03/07/26-04/07/26", "32 Day Billing Cycle from 02/28/2026 to 03/31/2026",
# "Opening/Closing Date 07/28/26 - 08/27/26". \D keeps the match from jumping across other numbers.
PERIOD_RE = re.compile(
    rf"(?:Billing\s+(?:Cycle|Period)|Statement\s+Period|Opening/Closing\s+Date)\D{{0,40}}?{_DATE}\s*(?:to|through|-|–|—)\s*{_DATE}",
    re.I,
)
CLOSING_DATE_RE = re.compile(rf"(?:Statement\s+)?Closing\s+Date\s*:?\s*{_DATE}", re.I)


def extract_statement_info(file_bytes: bytes) -> dict:
    """Best-effort read of a statement PDF's summary box. Every field is None when it can't be
    found -- statement layouts vary, so callers must treat this as a suggestion to show the user."""
    info: dict = {"period_start": None, "period_end": None, "previous_balance": None, "new_balance": None}
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages[:3])
    except Exception:
        return info

    new_asof: Optional[str] = None
    for m in NEW_RE.finditer(text):
        info["new_balance"] = parse_amount(m.group(2))
        new_asof = m.group(1)
        break
    for m in PREVIOUS_RE.finditer(text):
        info["previous_balance"] = parse_amount(m.group(2))
        break

    period = PERIOD_RE.search(text)
    if period:
        info["period_start"] = parse_date(period.group(1))
        info["period_end"] = parse_date(period.group(2))
    if info["period_end"] is None:
        closing = CLOSING_DATE_RE.search(text)
        raw_end = closing.group(1) if closing else new_asof
        info["period_end"] = parse_date(raw_end) if raw_end else None
    return info
