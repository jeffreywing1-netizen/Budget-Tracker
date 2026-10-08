import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class NormalizedTransaction:
    date: str  # ISO YYYY-MM-DD
    posted_date: Optional[str]
    description: str
    raw_description: str
    amount: float  # negative = money out (spend), positive = money in (payment/credit/refund)


def make_dedupe_hash(account_id: int, txn: NormalizedTransaction) -> str:
    from app.categorize import normalize_description
    key = f"{account_id}|{txn.date}|{txn.amount:.2f}|{normalize_description(txn.raw_description)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ImportError_(Exception):
    """Raised when a file can't be parsed at all (wrong format, unreadable, etc.)."""
