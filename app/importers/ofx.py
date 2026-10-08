import io

from ofxparse import OfxParser

from .base import NormalizedTransaction, ImportError_


def parse_ofx(file_bytes: bytes) -> list[NormalizedTransaction]:
    try:
        ofx = OfxParser.parse(io.BytesIO(file_bytes))
    except Exception as exc:  # ofxparse raises assorted exception types
        raise ImportError_(f"Could not parse OFX/QFX file: {exc}") from exc

    results: list[NormalizedTransaction] = []
    accounts = getattr(ofx, "accounts", None) or ([ofx.account] if getattr(ofx, "account", None) else [])
    for account in accounts:
        statement = getattr(account, "statement", None)
        if not statement:
            continue
        for txn in statement.transactions:
            raw_desc = (txn.payee or txn.memo or "").strip()
            if not raw_desc:
                raw_desc = "(no description)"
            amount = float(txn.amount)
            results.append(
                NormalizedTransaction(
                    date=txn.date.date().isoformat(),
                    posted_date=None,
                    description=" ".join(raw_desc.split()),
                    raw_description=raw_desc,
                    amount=amount,
                )
            )
    return results
