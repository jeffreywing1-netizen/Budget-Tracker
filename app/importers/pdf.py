import re
from collections import Counter
from datetime import date
from typing import Optional

import pdfplumber

from .base import NormalizedTransaction, ImportError_
from .csv_generic import parse_amount

# "MM/DD/YYYY  Description text  -$12.34"  or  "MM/DD  Description text  12.34"
# The amount is not end-anchored: column-based PDF text extraction sometimes merges an
# unrelated fragment onto the end of a real transaction line (e.g. a nearby rewards-summary
# box), so this matches the first dollar amount after the description and ignores anything
# further down the line.
LINE_RE = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(?P<desc>.+?)\s+"
    r"(?P<amount>\(?-?\$?[\d,]+\.\d{2}\)?)(?:\s|$)"
)
# A dated line whose amount isn't on it: the statement wrapped the transaction, so the amount
# (and the rest of the description, e.g. a foreign-currency note or the merchant's legal name)
# is on the line(s) below. Only "$"-prefixed amounts count on those follow-on lines, so a bare
# "56.54 POUND STERLING" figure isn't mistaken for the charge.
DATE_START_RE = re.compile(r"^(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(?P<desc>\S.*)$")
CONTINUATION_RE = re.compile(r"^(?P<desc>.*?)\s*(?P<amount>\(?-?\$[\d,]+\.\d{2}\)?)(?:\s|$)")
CONTINUATION_LOOKAHEAD = 2
YEAR_RE = re.compile(r"\b(20\d{2})\b")
# Year embedded in an actual date (MM/DD/YY or MM/DD/YYYY) -- used in preference to YEAR_RE
# since a bare 4-digit-year match also fires on unrelated boilerplate like "Member Since 2025",
# which can outnumber and outrank the statement's real year in a tied/near-tied count.
DATED_YEAR_RE = re.compile(r"\d{1,2}/\d{1,2}/(\d{2,4})")

# Standard credit-card/bank statement boilerplate that occasionally gets mashed together
# with a nearby date and dollar amount by column-based PDF text extraction (e.g. a payment
# coupon stub), producing a line that looks like a transaction but isn't one.
NON_TRANSACTION_PHRASES = {
    "NEW BALANCE", "PREVIOUS BALANCE", "CURRENT BALANCE", "STATEMENT BALANCE",
    "MINIMUM PAYMENT DUE", "TOTAL MINIMUM PAYMENT DUE", "PAYMENT DUE DATE",
    "AVAILABLE CREDIT", "AVAILABLE CASH", "CREDIT LIMIT", "CASH ADVANCE LIMIT",
    "AMOUNT ENCLOSED", "PAST DUE AMOUNT",
}


# Statements group their transactions under titles that say which way the money went, which is
# far more reliable than guessing from how the amounts are printed (a statement with a single
# payment and nothing else has no majority to vote with). Titles are matched at the start of a
# line, since column-based extraction can merge a neighbouring column's text onto the end
# ("Standard Purchases Total Costco Cash Back Rewards Balance").
CREDIT, CHARGE = "credit", "charge"
# Unambiguous multi-word titles: whatever follows on the line is ignored.
_STRONG_TITLES = {
    "payments, credits and adjustments": CREDIT,
    "payments and other credits": CREDIT,
    "payments and credits": CREDIT,
    "credits and adjustments": CREDIT,
    "other credits": CREDIT,
    "standard purchases": CHARGE,
    "purchases and other debits": CHARGE,
    "purchases and other charges": CHARGE,
    "other charges": CHARGE,
    "fees charged": CHARGE,
    "interest charged": CHARGE,
    "cash advances": CHARGE,
}
# Bare one-word titles also start ordinary sentences ("Payments Other Than By Mail", "Payments must
# be received by ..."), so those only count when the line is nothing but the title and, optionally,
# a section total.
_WEAK_TITLES = {"payments": CREDIT, "credits": CREDIT, "purchases": CHARGE, "fees": CHARGE, "interest": CHARGE}
_TITLE_TOTAL_RE = re.compile(r"^\s*[-+]?\s*\(?\$?\s*[\d,]+\.\d{2}\)?\s*$")


def _section_title(line: str) -> Optional[str]:
    """CREDIT or CHARGE when the line is a section title such as "Payments, Credits and Adjustments"
    or "Standard Purchases", else None."""
    low = " ".join(line.lower().split())
    for title, kind in _STRONG_TITLES.items():
        if low.startswith(title):
            return kind
    for title, kind in _WEAK_TITLES.items():
        if low.startswith(title):
            rest = low[len(title):]
            if not rest.strip() or _TITLE_TOTAL_RE.match(rest):
                return kind
    return None


def _guess_default_year(text: str) -> int:
    dated_years = [int(y) + 2000 if len(y) == 2 else int(y) for y in DATED_YEAR_RE.findall(text)]
    if dated_years:
        return Counter(dated_years).most_common(1)[0][0]
    years = YEAR_RE.findall(text)
    if years:
        return int(Counter(years).most_common(1)[0][0])
    return date.today().year


def _guess_default_month(text: str) -> Optional[int]:
    """The statement's typical/closing month, from full dates (MM/DD/YY). Used to detect
    a billing cycle that wraps a year boundary (e.g. a "January" statement with purchases
    dated in December)."""
    months = [int(m) for m, d, y in re.findall(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)]
    if not months:
        return None
    return Counter(months).most_common(1)[0][0]


def _parse_line_date(raw: str, default_year: int, default_month: Optional[int] = None) -> Optional[str]:
    parts = raw.split("/")
    try:
        if len(parts) == 3:
            month, day, year = parts
            year = int(year)
            if year < 100:
                year += 2000
        elif len(parts) == 2:
            month, day = parts
            year = default_year
            # A billing cycle closing early in the year (e.g. January) commonly starts in
            # December of the prior year -- without this, a 12/xx transaction in a
            # January-closing statement would silently land a year in the future.
            if default_month is not None and int(month) >= 11 and default_month <= 2:
                year -= 1
        else:
            return None
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def extract_table_from_pdf(file_bytes: bytes) -> Optional[tuple[list[str], list[list[str]]]]:
    """Looks for a real table (e.g. a bank's 'print statement' export) with a header row
    that has a date-like column. Returns (headers, rows) for the largest such table found,
    or None if nothing table-shaped is there -- callers should fall back to line scanning."""
    import io

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            best: Optional[tuple[list[str], list[list[str]]]] = None
            for page in pdf.pages:
                for table in page.extract_tables():
                    if not table or len(table) < 2:
                        continue
                    header = [("" if c is None else str(c)).strip() for c in table[0]]
                    # Require a genuine "Date" column, not just any header containing the
                    # substring "date" -- e.g. "2026 Year-to-Date Fees and Interest" would
                    # otherwise false-match a summary box and hijack the whole import.
                    if len(header) < 3:
                        continue
                    if not any(h.lower() in ("date", "trans date", "transaction date", "posted date", "post date")
                               for h in header):
                        continue
                    rows = [
                        [("" if c is None else str(c)).strip() for c in row]
                        for row in table[1:]
                    ]
                    if best is None or len(rows) > len(best[1]):
                        best = (header, rows)
            return best
    except Exception:
        return None


def parse_pdf(file_bytes: bytes) -> list[NormalizedTransaction]:
    import io

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        raise ImportError_(f"Could not read PDF file: {exc}") from exc

    if not full_text.strip():
        raise ImportError_("No extractable text found in PDF (it may be a scanned image).")

    default_year = _guess_default_year(full_text)
    default_month = _guess_default_month(full_text)
    results: list[NormalizedTransaction] = []
    # The statement section (CREDIT / CHARGE / None) each result was listed under, parallel to `results`.
    sections: list[Optional[str]] = []
    section: Optional[str] = None

    lines = [raw.strip() for raw in full_text.splitlines()]
    for index, line in enumerate(lines):
        if not line:
            continue
        title = _section_title(line)
        if title is not None:
            section = title
            continue

        match = LINE_RE.match(line)
        if match:
            raw_desc, raw_amount = match.group("desc"), match.group("amount")
        else:
            wrapped = _wrapped_transaction(lines, index)
            if wrapped is None:
                continue
            match, raw_desc, raw_amount = wrapped

        date_val = _parse_line_date(match.group("date"), default_year, default_month)
        if date_val is None:
            continue
        amount = parse_amount(raw_amount)
        if amount is None or amount == 0:
            continue
        raw_desc = " ".join(raw_desc.split())
        if not raw_desc:
            continue
        if raw_desc.upper() in NON_TRANSACTION_PHRASES:
            continue
        results.append(
            NormalizedTransaction(
                date=date_val,
                posted_date=None,
                description=raw_desc,
                raw_description=raw_desc,
                amount=amount,
            )
        )
        sections.append(section)

    if not results:
        raise ImportError_(
            "Couldn't find any transaction lines in this PDF automatically. "
            "Try exporting a CSV from your bank's website instead, if available."
        )

    _apply_signs(results, sections)
    return results


def _wrapped_transaction(lines: list[str], index: int):
    """For a dated line with no amount, looks on the next line(s) for the amount and the rest of the
    description. Returns (date match, description, amount text), or None if this isn't a wrapped
    transaction."""
    start = DATE_START_RE.match(lines[index])
    if start is None:
        return None
    parts = [start.group("desc")]
    for follow in lines[index + 1:index + 1 + CONTINUATION_LOOKAHEAD]:
        # A new dated line or a section title means the transaction never got an amount.
        if not follow or DATE_START_RE.match(follow) or _section_title(follow) is not None:
            return None
        found = CONTINUATION_RE.match(follow)
        if found:
            if found.group("desc"):
                parts.append(found.group("desc"))
            return start, " ".join(parts), found.group("amount")
        parts.append(follow)
    return None


def _apply_signs(results: list[NormalizedTransaction], sections: list[Optional[str]]) -> None:
    """Converts the printed amounts to our convention (spend = negative, payments/credits = positive).

    A line under a recognized section title takes its direction from that title: charges (purchases,
    fees, interest) are spend, and payments/credits are money in, however the amount happens to be
    printed. A charge printed negative is a reversal, so it comes out positive. Lines that aren't under
    any recognized title fall back to guessing from how the amounts are printed."""
    unlabeled: list[NormalizedTransaction] = []
    for txn, section in zip(results, sections):
        if section == CREDIT:
            txn.amount = abs(txn.amount)
        elif section == CHARGE:
            txn.amount = -txn.amount
        else:
            unlabeled.append(txn)

    # Most statement layouts list purchases unsigned and only mark payments/credits
    # with a "-" or parens, so purchases (usually the majority of lines) come out
    # positive. Our convention is spend = negative, so if under half the parsed
    # amounts are negative, the source is using the opposite convention -- flip all.
    if unlabeled:
        negative_count = sum(1 for t in unlabeled if t.amount < 0)
        if negative_count / len(unlabeled) < 0.5:
            for t in unlabeled:
                t.amount = -t.amount
