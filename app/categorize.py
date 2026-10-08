import re
import sqlite3


def normalize_description(raw: str) -> str:
    return " ".join(raw.upper().split())


def categorize(conn: sqlite3.Connection, description: str) -> int:
    normalized = normalize_description(description)
    rules = conn.execute(
        "SELECT pattern, match_type, category_id FROM category_rules "
        "ORDER BY priority DESC, id ASC"
    ).fetchall()

    for rule in rules:
        pattern = rule["pattern"]
        if rule["match_type"] == "regex":
            try:
                if re.search(pattern, normalized):
                    return rule["category_id"]
            except re.error:
                continue
        else:
            if pattern.upper() in normalized:
                return rule["category_id"]

    uncategorized = conn.execute(
        "SELECT id FROM categories WHERE name = 'Uncategorized'"
    ).fetchone()
    return uncategorized["id"]


def merchant_token(raw_description: str) -> str:
    """Heuristic merchant key used when a user's manual correction creates a new rule:
    the first few words of the normalized description, which usually captures the
    merchant name without leading/trailing store numbers or reference codes.

    Some merchants (e.g. Sam's Club) prefix every line with a per-transaction ID that
    mixes letters and digits (e.g. "8521333L20168ESR3"), which isn't purely numeric so
    it wouldn't otherwise be recognized as noise -- left unfiltered, every correction
    on that merchant would bake in a unique code and never match anything again. Such
    a token is skipped (not just stopped on), so a real merchant word appearing after
    it -- "SAM'S CLUB" -- still gets captured."""
    normalized = normalize_description(raw_description)
    words = normalized.split()
    trimmed = []
    for w in words[:4]:
        if re.fullmatch(r"[0-9#*]+", w):
            break
        if len(w) >= 10 and re.search(r"[0-9]", w) and re.search(r"[A-Z]", w):
            continue
        trimmed.append(w)
    return " ".join(trimmed) if trimmed else normalized


def add_correction_rule(conn: sqlite3.Connection, raw_description: str, category_id: int) -> None:
    pattern = merchant_token(raw_description)
    if not pattern:
        return
    existing = conn.execute(
        "SELECT id FROM category_rules WHERE pattern = ? AND category_id = ?",
        (pattern, category_id),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO category_rules (pattern, match_type, category_id, priority) "
        "VALUES (?, 'contains', ?, 200)",
        (pattern, category_id),
    )
    conn.commit()
