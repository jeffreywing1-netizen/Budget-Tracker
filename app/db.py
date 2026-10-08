import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "budget.db"
BACKUP_DIR = DB_PATH.parent / "backups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    institution TEXT NOT NULL DEFAULT 'other',
    account_type TEXT NOT NULL DEFAULT 'credit',
    last4 TEXT,
    column_mapping TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS category_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'contains',
    category_id INTEGER NOT NULL REFERENCES categories(id),
    priority INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    row_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    date TEXT NOT NULL,
    posted_date TEXT,
    description TEXT NOT NULL,
    raw_description TEXT NOT NULL,
    amount REAL NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    import_batch_id INTEGER REFERENCES import_batches(id),
    dedupe_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category_id);

CREATE TABLE IF NOT EXISTS transaction_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    amount REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_splits_transaction ON transaction_splits(transaction_id);

-- One row per statement (account + closing date). The balances are as printed on the
-- statement (for a credit card, the amount owed), so the statement's own net change is
-- new_balance - previous_balance. period_start / previous_balance may be NULL, meaning
-- "continue from the previous statement on file for this account" (see reconcile).
CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    period_start TEXT,
    period_end TEXT NOT NULL,
    previous_balance REAL,
    new_balance REAL NOT NULL,
    source_file TEXT,
    pdf_lines INTEGER,  -- transaction lines the statement PDF listed, when it was read from a PDF
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, period_end)
);

-- One row per (transaction, category) allocation: a split transaction contributes one row
-- per split; an unsplit transaction falls back to its own category_id/amount via COALESCE.
-- Every category-level report (month/year summaries) reads from this view instead of the
-- transactions table directly, so splits are automatically reflected everywhere.
CREATE VIEW IF NOT EXISTS transaction_category_amounts AS
SELECT
    t.id AS transaction_id,
    t.account_id,
    t.date,
    COALESCE(s.category_id, t.category_id) AS category_id,
    COALESCE(s.amount, t.amount) AS amount
FROM transactions t
LEFT JOIN transaction_splits s ON s.transaction_id = t.id;

-- One row per (category, year, month). For a 'monthly' category this is that month's
-- budget; for an 'annual' category it's the confirmed annual total as of that month
-- (the UI derives amount/12 for the amortized monthly contribution). Re-saved every
-- month by design, not a one-time set-and-forget value.
CREATE TABLE IF NOT EXISTS category_budgets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(category_id, year, month)
);
"""

DEFAULT_CATEGORIES = [
    ("Groceries", "#4CAF50", False),
    ("Dining", "#FF9800", False),
    ("Gas & Transportation", "#795548", False),
    ("Utilities", "#607D8B", False),
    ("Subscriptions", "#9C27B0", False),
    ("Shopping", "#E91E63", False),
    ("Entertainment", "#3F51B5", False),
    ("Health", "#009688", False),
    ("Travel", "#00BCD4", False),
    ("Housing", "#8D6E63", False),
    ("Insurance", "#5D4037", False),
    ("Fees & Interest", "#F44336", False),
    ("Income & Payments", "#2196F3", False),
    ("Transfers", "#78909C", True),
    ("Uncategorized", "#9E9E9E", False),
]

# (category_name, [keywords]) -- seeded as "contains" rules, priority 100
DEFAULT_RULES = {
    "Groceries": ["WALMART", "WAL-MART", "KROGER", "SAFEWAY", "WHOLE FOODS", "TRADER JOE",
                  "ALDI", "PUBLIX", "COSTCO", "STOP & SHOP", "KEY FOOD", "FOODTOWN", "SUPERMARKET"],
    "Dining": ["MCDONALD", "STARBUCKS", "CHIPOTLE", "DOORDASH", "GRUBHUB", "UBER EATS",
               "RESTAURANT", "PIZZA", "CHICK-FIL-A", "DUNKIN", "COFFEE", "TACO", "WENDY", "BURGER"],
    "Gas & Transportation": ["SHELL", "EXXON", "CHEVRON", " BP ", "MOBIL", "SUNOCO", "UBER",
                              "LYFT", "MTA", "TRANSIT", "PARKING", "GAS STATION"],
    "Utilities": ["CON ED", "CONEDISON", "PSEG", "NATIONAL GRID", "VERIZON", "AT&T", "T-MOBILE",
                  "COMCAST", "XFINITY", "SPECTRUM", "WATER DEPT", "ELECTRIC"],
    "Subscriptions": ["NETFLIX", "SPOTIFY", "HULU", "DISNEY+", "AMAZON PRIME", "APPLE.COM/BILL",
                       "ICLOUD", "YOUTUBE PREMIUM", "HBO"],
    "Shopping": ["AMAZON", "EBAY", "BEST BUY", "HOME DEPOT", "LOWE'S", "LOWES", "MACY", "TARGET",
                 "IKEA", "ETSY"],
    "Entertainment": ["AMC", "REGAL", "CINEMA", "TICKETMASTER", "STEAM", "PLAYSTATION", "XBOX"],
    "Health": ["CVS", "WALGREENS", "PHARMACY", "DENTAL", "MEDICAL", "DOCTOR", "URGENT CARE"],
    "Travel": ["DELTA", "AMERICAN AIRLINES", "UNITED AIRLINES", "SOUTHWEST", "MARRIOTT",
               "HILTON", "AIRBNB", "EXPEDIA", "JETBLUE"],
    "Housing": ["RENT PAYMENT", "MORTGAGE"],
    "Insurance": ["GEICO", "PROGRESSIVE", "ALLSTATE", "STATE FARM", "INSURANCE"],
    "Fees & Interest": ["INTEREST CHARGE", "LATE FEE", "ANNUAL FEE", "OVERDRAFT"],
    "Income & Payments": ["DIRECT DEPOSIT", "PAYROLL"],
    # Credit card bill payments / balance transfers between the user's own accounts --
    # not real income or spending, so this category is flagged is_transfer and excluded
    # from spend/income totals (see the Transfers row in DEFAULT_CATEGORIES).
    "Transfers": ["PAYMENT THANK YOU", "AUTOMATIC PAYMENT", "AUTOPAY", "ONLINE PAYMENT",
                  "INTERNET TRANSFER", "ONLINE TRANSFER"],
}


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing_cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "categories", "is_transfer", "is_transfer INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "import_batches", "statement_total", "statement_total REAL")
        _ensure_column(conn, "categories", "group_id", "group_id INTEGER REFERENCES category_groups(id)")
        _ensure_column(conn, "category_groups", "color", "color TEXT NOT NULL DEFAULT '#9E9E9E'")
        _ensure_column(conn, "categories", "budget_frequency", "budget_frequency TEXT NOT NULL DEFAULT 'monthly'")
        # Set when a statement PDF lists this transaction: it then belongs to that statement no
        # matter what date it carries (a card statement assigns purchases to a cycle by posting
        # date, which can be a few days after the purchase date the PDF shows).
        _ensure_column(conn, "transactions", "statement_id",
                       "statement_id INTEGER REFERENCES statements(id) ON DELETE SET NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_statement ON transactions(statement_id)")
        conn.commit()

        # Only seed default categories on a truly empty table (first-ever run). Checking
        # "does this name exist" instead would silently resurrect a category the user
        # deliberately deleted or merged away on every subsequent app restart.
        category_count = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
        if category_count == 0:
            for name, color, is_transfer in DEFAULT_CATEGORIES:
                conn.execute(
                    "INSERT INTO categories (name, color, is_transfer) VALUES (?, ?, ?)",
                    (name, color, int(is_transfer)),
                )
            conn.commit()

        rule_count = conn.execute("SELECT COUNT(*) AS c FROM category_rules").fetchone()["c"]
        if rule_count == 0:
            cat_ids = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM categories")}
            for cat_name, keywords in DEFAULT_RULES.items():
                cat_id = cat_ids.get(cat_name)
                if cat_id is None:
                    continue
                for kw in keywords:
                    conn.execute(
                        "INSERT INTO category_rules (pattern, match_type, category_id, priority) "
                        "VALUES (?, 'contains', ?, 100)",
                        (kw, cat_id),
                    )
            conn.commit()
    finally:
        conn.close()


def backup_db() -> Path:
    """Copies the live database to data/backups/ before a destructive operation."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"budget_backup_{timestamp}.db"
    shutil.copy2(DB_PATH, backup_path)
    return backup_path
