"""
Best-effort header presets for institutions with well-known CSV export formats.
If a file's headers don't confidently match a preset, the importer falls back
to the generic column-mapping screen -- this is expected for Popular Bank and
Synchrony until we see real sample exports from them.
"""


def _norm(headers: list[str]) -> list[str]:
    return [h.strip().lower() for h in headers]


def detect_preset(institution: str, headers: list[str]) -> dict | None:
    normalized = _norm(headers)

    if institution == "chase":
        # Chase credit card export: Transaction Date, Post Date, Description, Category, Type, Amount
        # Amount is negative for purchases, positive for payments/credits/refunds.
        required = ["transaction date", "description", "amount"]
        if all(r in normalized for r in required):
            return {
                "date_col": headers[normalized.index("transaction date")],
                "posted_date_col": headers[normalized.index("post date")] if "post date" in normalized else None,
                "description_col": headers[normalized.index("description")],
                "amount_col": headers[normalized.index("amount")],
                "amount_sign": "spend_negative",
            }

    if institution == "capital_one":
        # Capital One export: Transaction Date, Posted Date, Card No., Description, Category, Debit, Credit
        required = ["transaction date", "description"]
        if all(r in normalized for r in required) and ("debit" in normalized or "credit" in normalized):
            return {
                "date_col": headers[normalized.index("transaction date")],
                "posted_date_col": headers[normalized.index("posted date")] if "posted date" in normalized else None,
                "description_col": headers[normalized.index("description")],
                "debit_col": headers[normalized.index("debit")] if "debit" in normalized else None,
                "credit_col": headers[normalized.index("credit")] if "credit" in normalized else None,
            }

    return None
