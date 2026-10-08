import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db, get_connection, backup_db
from app.auth import PasswordGate
from app.categorize import categorize, add_correction_rule, normalize_description
from app.models import (
    AccountCreate, CategoryCreate, CategoryUpdate, RuleCreate, TransactionUpdate, SplitsUpdate, TrimRequest,
    ReassignRequest, StatementTotalUpdate, CategoryGroupCreate, CategoryGroupUpdate, BudgetSaveRequest,
    StatementSave, ReconcileRequest,
)
from app.importers.base import NormalizedTransaction, ImportError_, make_dedupe_hash
from app.importers import csv_generic
from app.importers.presets import detect_preset
from app.importers.ofx import parse_ofx
from app.importers.pdf import parse_pdf, extract_table_from_pdf
from app.importers.statement_info import extract_statement_info

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Budget Tracker")
app.add_middleware(PasswordGate)
init_db()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------- Accounts ----------

@app.get("/api/accounts")
def list_accounts():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/accounts")
def create_account(payload: AccountCreate):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO accounts (name, institution, account_type, last4) VALUES (?, ?, ?, ?)",
            (payload.name, payload.institution, payload.account_type, payload.last4),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


# ---------- Category groups ----------

@app.get("/api/category_groups")
def list_category_groups():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM category_groups ORDER BY name").fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/category_groups")
def create_category_group(payload: CategoryGroupCreate):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO category_groups (name, color) VALUES (?, ?)", (payload.name, payload.color)
        )
        conn.commit()
        return {"id": cur.lastrowid}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A group with that name already exists")
    finally:
        conn.close()


@app.put("/api/category_groups/{group_id}")
def update_category_group(group_id: int, payload: CategoryGroupUpdate):
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM category_groups WHERE id = ?", (group_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Group not found")
        conn.execute(
            "UPDATE category_groups SET name = ?, color = ? WHERE id = ?",
            (payload.name, payload.color, group_id),
        )
        conn.commit()
        return {"ok": True}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A group with that name already exists")
    finally:
        conn.close()


@app.delete("/api/category_groups/{group_id}")
def delete_category_group(group_id: int):
    """Deletes the group; categories that belonged to it just become ungrouped, not deleted."""
    conn = get_connection()
    try:
        conn.execute("UPDATE categories SET group_id = NULL WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM category_groups WHERE id = ?", (group_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------- Categories ----------

@app.get("/api/categories")
def list_categories():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT c.*, g.name AS group_name FROM categories c "
            "LEFT JOIN category_groups g ON g.id = c.group_id "
            "ORDER BY c.name"
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/categories")
def create_category(payload: CategoryCreate):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO categories (name, color, is_transfer, group_id, budget_frequency) VALUES (?, ?, ?, ?, ?)",
            (payload.name, payload.color, int(payload.is_transfer), payload.group_id, payload.budget_frequency),
        )
        conn.commit()
        return {"id": cur.lastrowid}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A category with that name already exists")
    finally:
        conn.close()


@app.put("/api/categories/{category_id}")
def update_category(category_id: int, payload: CategoryUpdate):
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Category not found")
        conn.execute(
            "UPDATE categories SET name = ?, color = ?, is_transfer = ?, group_id = ?, budget_frequency = ? "
            "WHERE id = ?",
            (payload.name, payload.color, int(payload.is_transfer), payload.group_id,
             payload.budget_frequency, category_id),
        )
        conn.commit()
        return {"ok": True}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "A category with that name already exists")
    finally:
        conn.close()


@app.post("/api/categories/{source_id}/merge_into/{target_id}")
def merge_categories(source_id: int, target_id: int):
    """Moves every transaction, split allocation, and rule from source_id onto target_id,
    then deletes source_id. Used to consolidate near-duplicate categories (e.g. 'Dining'
    and 'Restaurants') without losing history."""
    if source_id == target_id:
        raise HTTPException(400, "Can't merge a category into itself")
    conn = get_connection()
    try:
        source = conn.execute("SELECT id FROM categories WHERE id = ?", (source_id,)).fetchone()
        target = conn.execute("SELECT id FROM categories WHERE id = ?", (target_id,)).fetchone()
        if not source or not target:
            raise HTTPException(404, "Category not found")

        txn_count = conn.execute(
            "UPDATE transactions SET category_id = ? WHERE category_id = ?", (target_id, source_id)
        ).rowcount
        split_count = conn.execute(
            "UPDATE transaction_splits SET category_id = ? WHERE category_id = ?", (target_id, source_id)
        ).rowcount
        rule_count = conn.execute(
            "UPDATE category_rules SET category_id = ? WHERE category_id = ?", (target_id, source_id)
        ).rowcount

        # Drop exact-duplicate rules created by the merge (same pattern/type now both
        # pointing at target_id), keeping the earliest of each.
        conn.execute(
            "DELETE FROM category_rules WHERE category_id = ? AND id NOT IN ("
            "  SELECT MIN(id) FROM category_rules WHERE category_id = ? GROUP BY pattern, match_type"
            ")",
            (target_id, target_id),
        )

        conn.execute("DELETE FROM categories WHERE id = ?", (source_id,))
        conn.commit()
        return {"transactions_moved": txn_count, "splits_moved": split_count, "rules_moved": rule_count}
    finally:
        conn.close()


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int):
    conn = get_connection()
    try:
        txn_count = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE category_id = ?", (category_id,)
        ).fetchone()["c"]
        rule_count = conn.execute(
            "SELECT COUNT(*) AS c FROM category_rules WHERE category_id = ?", (category_id,)
        ).fetchone()["c"]
        if txn_count or rule_count:
            raise HTTPException(
                400,
                f"Can't delete: {txn_count} transaction(s) and {rule_count} rule(s) still use this category. "
                "Recategorize or delete those first.",
            )
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------- Rules ----------

@app.get("/api/rules")
def list_rules():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT r.id, r.pattern, r.match_type, r.priority, r.category_id, "
            "c.name AS category_name, c.color AS category_color "
            "FROM category_rules r JOIN categories c ON c.id = r.category_id "
            "ORDER BY r.priority DESC, r.id DESC"
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _apply_pattern_to_transactions(conn: sqlite3.Connection, pattern: str, match_type: str, category_id: int) -> int:
    """Recategorizes every existing transaction whose description matches this pattern.
    Any existing split on a matched transaction is cleared, since it's being reassigned
    to a single category."""
    if match_type == "regex":
        import re
        try:
            compiled = re.compile(pattern)
        except re.error:
            return 0
        candidates = conn.execute("SELECT id, description FROM transactions").fetchall()
        matched_ids = [r["id"] for r in candidates if compiled.search(normalize_description(r["description"]))]
        if not matched_ids:
            return 0
        placeholders = ",".join("?" * len(matched_ids))
        conn.execute(
            f"UPDATE transactions SET category_id = ? WHERE id IN ({placeholders})",
            (category_id, *matched_ids),
        )
        conn.execute(
            f"DELETE FROM transaction_splits WHERE transaction_id IN ({placeholders})",
            matched_ids,
        )
        conn.commit()
        return len(matched_ids)
    else:
        cur = conn.execute(
            "UPDATE transactions SET category_id = ? WHERE UPPER(description) LIKE ?",
            (category_id, f"%{pattern.upper()}%"),
        )
        conn.execute(
            "DELETE FROM transaction_splits WHERE transaction_id IN "
            "(SELECT id FROM transactions WHERE UPPER(description) LIKE ?)",
            (f"%{pattern.upper()}%",),
        )
        conn.commit()
        return cur.rowcount


@app.post("/api/rules")
def create_rule(payload: RuleCreate):
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO category_rules (pattern, match_type, category_id, priority) VALUES (?, ?, ?, ?)",
            (payload.pattern, payload.match_type, payload.category_id, payload.priority),
        )
        conn.commit()
        updated = 0
        if payload.apply_to_existing:
            updated = _apply_pattern_to_transactions(conn, payload.pattern, payload.match_type, payload.category_id)
        return {"id": cur.lastrowid, "updated": updated}
    finally:
        conn.close()


@app.post("/api/rules/{rule_id}/apply")
def apply_rule(rule_id: int):
    conn = get_connection()
    try:
        rule = conn.execute("SELECT * FROM category_rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            raise HTTPException(404, "Rule not found")
        updated = _apply_pattern_to_transactions(conn, rule["pattern"], rule["match_type"], rule["category_id"])
        return {"updated": updated}
    finally:
        conn.close()


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------- Import ----------

def _mapping_valid(mapping: dict, headers: list[str]) -> bool:
    if not mapping.get("date_col") or mapping["date_col"] not in headers:
        return False
    if not mapping.get("description_col") or mapping["description_col"] not in headers:
        return False
    has_amount = mapping.get("amount_col") in headers
    has_split = mapping.get("debit_col") in headers or mapping.get("credit_col") in headers
    return bool(has_amount or has_split)


def _parse_file(
    filename: str,
    file_bytes: bytes,
    mapping: Optional[dict],
    institution: str,
    stored_mapping: Optional[dict] = None,
):
    """Returns (status, data) where status is 'needs_mapping' or 'parsed'."""

    def _resolve_tabular(headers: list[str], rows: list[list[str]]):
        nonlocal mapping
        if mapping is None:
            preset = detect_preset(institution, headers)
            if preset is not None:
                mapping = preset
            elif stored_mapping and _mapping_valid(stored_mapping, headers):
                mapping = stored_mapping
            else:
                return "needs_mapping", {"headers": headers, "sample_rows": rows[:5]}
        txns = csv_generic.apply_mapping(headers, rows, mapping)
        return "parsed", {"transactions": txns, "mapping": mapping}

    lower = filename.lower()
    if lower.endswith((".csv", ".xlsx", ".xlsm")):
        headers, rows = csv_generic.read_tabular(file_bytes, filename)
        if not headers:
            raise ImportError_("The file appears to be empty.")
        return _resolve_tabular(headers, rows)

    elif lower.endswith((".ofx", ".qfx")):
        txns = parse_ofx(file_bytes)
        return "parsed", {"transactions": txns, "mapping": None}

    elif lower.endswith(".pdf"):
        table = extract_table_from_pdf(file_bytes)
        if table is not None:
            headers, rows = table
            return _resolve_tabular(headers, rows)
        txns = parse_pdf(file_bytes)
        return "parsed", {"transactions": txns, "mapping": None}

    else:
        raise ImportError_(f"Unsupported file type: {filename}")


def _apply_correction_to_similar(conn: sqlite3.Connection, account_id: int,
                                 raw_description: str, category_id: int) -> None:
    """Saves a reusable rule for this merchant and re-categorizes the account's already-imported
    transactions that share it -- what "apply to similar" means everywhere it's offered."""
    from app.categorize import merchant_token
    add_correction_rule(conn, raw_description, category_id)
    token = merchant_token(raw_description)
    conn.execute(
        "UPDATE transactions SET category_id = ? "
        "WHERE account_id = ? AND UPPER(description) LIKE ?",
        (category_id, account_id, f"%{token}%"),
    )


@app.post("/api/import")
async def import_file(
    file: UploadFile = File(...),
    account_id: int = Form(...),
    mapping: Optional[str] = Form(None),
    commit: bool = Form(False),
    overrides: Optional[str] = Form(None),
):
    """overrides (commit only): JSON object keyed by each preview transaction's `key`, holding the
    edits made while reviewing the preview -- {category_id, splits, apply_similar} -- so the
    reviewed categorization is what actually gets saved."""
    conn = get_connection()
    try:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            raise HTTPException(404, "Account not found")

        file_bytes = await file.read()
        mapping_dict = json.loads(mapping) if mapping else None
        overrides_dict = json.loads(overrides) if overrides else {}
        stored_mapping = json.loads(account["column_mapping"]) if account["column_mapping"] else None

        try:
            status, data = _parse_file(
                file.filename, file_bytes, mapping_dict, account["institution"], stored_mapping
            )
        except ImportError_ as exc:
            raise HTTPException(400, str(exc))

        if status == "needs_mapping":
            return {"status": "needs_mapping", **data}

        used_mapping = data.get("mapping")
        if used_mapping and used_mapping != stored_mapping:
            conn.execute(
                "UPDATE accounts SET column_mapping = ? WHERE id = ?",
                (json.dumps(used_mapping), account_id),
            )
            conn.commit()

        txns: list[NormalizedTransaction] = data["transactions"]
        if not txns:
            return {"status": "parsed", "imported": 0, "duplicates": 0, "category_breakdown": []}

        # A statement PDF also carries the statement's own period and balances; capture them so the
        # Reconcile tab has something to compare the imported transactions against.
        statement_info = extract_statement_info(file_bytes) if file.filename.lower().endswith(".pdf") else None

        existing_hashes = {
            r["dedupe_hash"]
            for r in conn.execute("SELECT dedupe_hash FROM transactions WHERE account_id = ?", (account_id,))
        }

        # Work out which rows are new (vs. duplicates of what's already saved, or repeated in this
        # very file) up front, so the preview list and the commit are guaranteed to agree.
        fresh: list[tuple[str, NormalizedTransaction]] = []
        duplicates = 0
        for txn in txns:
            dedupe_hash = make_dedupe_hash(account_id, txn)
            if dedupe_hash in existing_hashes:
                duplicates += 1
                continue
            existing_hashes.add(dedupe_hash)
            fresh.append((dedupe_hash, txn))

        if commit:
            txn_by_key = {key: txn for key, txn in fresh}
            for key, o in overrides_dict.items():
                txn = txn_by_key.get(key)
                if txn is None:
                    continue
                splits = o.get("splits")
                if splits:
                    if len(splits) < 2:
                        raise HTTPException(400, f"A split needs at least two categories ({txn.description})")
                    if abs(sum(s["amount"] for s in splits) - txn.amount) > 0.01:
                        raise HTTPException(400, f"Split amounts don't add up to the transaction total ({txn.description})")

            # Corrections marked "apply to similar" go in first, so they also shape how the rest of
            # this file is categorized; each row's own explicit override is applied on top afterwards.
            for key, o in overrides_dict.items():
                txn = txn_by_key.get(key)
                if txn is not None and o.get("apply_similar") and o.get("category_id"):
                    _apply_correction_to_similar(conn, account_id, txn.raw_description, o["category_id"])

        breakdown: dict[int, dict] = {}
        preview_rows: list[dict] = []
        imported = 0
        batch_id = None

        if commit:
            cur = conn.execute(
                "INSERT INTO import_batches (account_id, filename, row_count) VALUES (?, ?, 0)",
                (account_id, file.filename),
            )
            batch_id = cur.lastrowid

        from app.categorize import merchant_token
        for dedupe_hash, txn in fresh:
            override = overrides_dict.get(dedupe_hash) if commit else None
            category_id = (override or {}).get("category_id") or categorize(conn, txn.description)
            splits = (override or {}).get("splits") or []

            if commit:
                try:
                    cur = conn.execute(
                        "INSERT INTO transactions "
                        "(account_id, date, posted_date, description, raw_description, amount, "
                        "category_id, import_batch_id, dedupe_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (account_id, txn.date, txn.posted_date, txn.description, txn.raw_description,
                         txn.amount, category_id, batch_id, dedupe_hash),
                    )
                except sqlite3.IntegrityError:
                    duplicates += 1
                    continue
                for s in splits:
                    conn.execute(
                        "INSERT INTO transaction_splits (transaction_id, category_id, amount) VALUES (?, ?, ?)",
                        (cur.lastrowid, s["category_id"], s["amount"]),
                    )
            else:
                preview_rows.append({
                    "key": dedupe_hash,
                    "date": txn.date,
                    "description": txn.description,
                    "amount": txn.amount,
                    "category_id": category_id,
                    "merchant_token": merchant_token(txn.raw_description),
                })

            imported += 1
            for cat_id, amt in ([(s["category_id"], s["amount"]) for s in splits] or [(category_id, txn.amount)]):
                cat = breakdown.setdefault(cat_id, {"category_id": cat_id, "amount": 0.0, "count": 0})
                cat["amount"] += amt
                cat["count"] += 1

        statement_result = None
        if statement_info is not None:
            if commit:
                statement_result = _record_statement_from_pdf(
                    conn, account_id, file.filename, statement_info,
                    [make_dedupe_hash(account_id, t) for t in txns],
                )[0]
            else:
                found = statement_info["period_end"] and statement_info["new_balance"] is not None
                statement_result = {**statement_info, "status": "preview" if found else "not_found"}

        if commit:
            conn.execute(
                "UPDATE import_batches SET row_count = ?, skipped_count = ? WHERE id = ?",
                (imported, duplicates, batch_id),
            )
            conn.commit()

        cat_rows = {r["id"]: row_to_dict(r) for r in conn.execute("SELECT * FROM categories")}
        category_breakdown = []
        for entry in sorted(breakdown.values(), key=lambda e: e["amount"]):
            cat = cat_rows.get(entry["category_id"], {"name": "Unknown", "color": "#999999"})
            category_breakdown.append({
                "category_id": entry["category_id"],
                "name": cat["name"],
                "color": cat["color"],
                "amount": round(entry["amount"], 2),
                "count": entry["count"],
            })

        return {
            "status": "committed" if commit else "preview",
            "batch_id": batch_id,
            "imported": imported,
            "duplicates": duplicates,
            "category_breakdown": category_breakdown,
            "transactions": preview_rows,
            "mapping": data.get("mapping"),
            "statement": statement_result,
        }
    finally:
        conn.close()


@app.get("/api/import_batches")
def list_import_batches():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT b.*, a.name AS account_name, "
            "COALESCE(SUM(t.amount), 0) AS calculated_total, "
            "COALESCE(SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END), 0) AS calculated_spend, "
            "COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 0) AS calculated_credit "
            "FROM import_batches b "
            "JOIN accounts a ON a.id = b.account_id "
            "LEFT JOIN transactions t ON t.import_batch_id = b.id "
            "GROUP BY b.id ORDER BY b.imported_at DESC"
        ).fetchall()
        batches = [row_to_dict(r) for r in rows]
        for b in batches:
            b["calculated_total"] = round(b["calculated_total"], 2)
            b["calculated_spend"] = round(b["calculated_spend"], 2)
            b["calculated_credit"] = round(b["calculated_credit"], 2)
            if b["statement_total"] is not None:
                b["difference"] = round(b["calculated_total"] - b["statement_total"], 2)
        return batches
    finally:
        conn.close()


@app.put("/api/import_batches/{batch_id}/statement_total")
def set_statement_total(batch_id: int, payload: StatementTotalUpdate):
    conn = get_connection()
    try:
        existing = conn.execute("SELECT id FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Import batch not found")
        conn.execute(
            "UPDATE import_batches SET statement_total = ? WHERE id = ?",
            (payload.statement_total, batch_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/import_batches/{batch_id}/reassign")
def reassign_batch(batch_id: int, payload: ReassignRequest):
    """Moves every transaction from an import batch to a different account -- for when
    a file was imported against the wrong account. Recomputes each transaction's dedupe
    hash under the new account so future re-imports still dedupe correctly."""
    conn = get_connection()
    try:
        batch = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(404, "Import batch not found")
        target_account = conn.execute(
            "SELECT id FROM accounts WHERE id = ?", (payload.account_id,)
        ).fetchone()
        if not target_account:
            raise HTTPException(404, "Target account not found")

        txns = conn.execute(
            "SELECT * FROM transactions WHERE import_batch_id = ?", (batch_id,)
        ).fetchall()

        updated = 0
        conflicts = 0
        for t in txns:
            norm = NormalizedTransaction(
                date=t["date"], posted_date=t["posted_date"],
                description=t["description"], raw_description=t["raw_description"],
                amount=t["amount"],
            )
            new_hash = make_dedupe_hash(payload.account_id, norm)
            try:
                conn.execute(
                    "UPDATE transactions SET account_id = ?, dedupe_hash = ? WHERE id = ?",
                    (payload.account_id, new_hash, t["id"]),
                )
                updated += 1
            except sqlite3.IntegrityError:
                conflicts += 1

        conn.execute(
            "UPDATE import_batches SET account_id = ? WHERE id = ?",
            (payload.account_id, batch_id),
        )
        conn.commit()
        return {"updated": updated, "conflicts": conflicts}
    finally:
        conn.close()


@app.delete("/api/import_batches/{batch_id}")
def undo_import_batch(batch_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM transactions WHERE import_batch_id = ?", (batch_id,))
        conn.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------- Transactions ----------

@app.get("/api/transactions/trim_preview")
def trim_preview(before: str):
    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE date < ?", (before,)
        ).fetchone()["c"]
        by_account = conn.execute(
            "SELECT a.name AS account_name, COUNT(*) AS c, MIN(t.date) AS earliest, MAX(t.date) AS latest "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE t.date < ? GROUP BY a.id ORDER BY c DESC",
            (before,),
        ).fetchall()
        return {"count": count, "by_account": [row_to_dict(r) for r in by_account]}
    finally:
        conn.close()


@app.post("/api/transactions/trim")
def trim_transactions(payload: TrimRequest):
    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE date < ?", (payload.before,)
        ).fetchone()["c"]
        if count == 0:
            return {"deleted": 0, "backup_path": None}
    finally:
        conn.close()

    backup_path = backup_db()

    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM transactions WHERE date < ?", (payload.before,))
        conn.commit()
        return {"deleted": cur.rowcount, "backup_path": str(backup_path)}
    finally:
        conn.close()


@app.get("/api/transactions")
def list_transactions(
    year: Optional[int] = None,
    month: Optional[int] = None,
    account_id: Optional[int] = None,
    category_id: Optional[int] = None,
    search: Optional[str] = None,
    import_batch_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    conn = get_connection()
    try:
        query = (
            "SELECT t.*, c.name AS category_name, c.color AS category_color, "
            "a.name AS account_name "
            "FROM transactions t "
            "LEFT JOIN categories c ON c.id = t.category_id "
            "LEFT JOIN accounts a ON a.id = t.account_id "
            "WHERE 1=1"
        )
        params: list = []
        if year is not None:
            query += " AND strftime('%Y', t.date) = ?"
            params.append(f"{year:04d}")
        if month is not None:
            query += " AND strftime('%m', t.date) = ?"
            params.append(f"{month:02d}")
        if account_id is not None:
            query += " AND t.account_id = ?"
            params.append(account_id)
        if date_from:
            query += " AND t.date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND t.date <= ?"
            params.append(date_to)
        if import_batch_id is not None:
            query += " AND t.import_batch_id = ?"
            params.append(import_batch_id)
        if category_id is not None:
            # A split transaction's own category_id is stale (never updated once split --
            # see set_transaction_splits), so match against its actual split allocations
            # instead when it has any; fall back to the plain column for unsplit rows.
            query += (
                " AND ("
                "  (t.category_id = ? AND NOT EXISTS ("
                "    SELECT 1 FROM transaction_splits s WHERE s.transaction_id = t.id"
                "  ))"
                "  OR EXISTS ("
                "    SELECT 1 FROM transaction_splits s2 "
                "    WHERE s2.transaction_id = t.id AND s2.category_id = ?"
                "  )"
                ")"
            )
            params.append(category_id)
            params.append(category_id)
        if search:
            query += " AND t.description LIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY t.date DESC, t.id DESC"

        rows = conn.execute(query, params).fetchall()
        txns = [row_to_dict(r) for r in rows]

        txn_ids = [t["id"] for t in txns]
        if txn_ids:
            placeholders = ",".join("?" * len(txn_ids))
            split_rows = conn.execute(
                f"SELECT s.transaction_id, s.category_id, s.amount, "
                f"c.name AS category_name, c.color AS category_color "
                f"FROM transaction_splits s JOIN categories c ON c.id = s.category_id "
                f"WHERE s.transaction_id IN ({placeholders})",
                txn_ids,
            ).fetchall()
            splits_by_txn: dict[int, list] = {}
            for sr in split_rows:
                splits_by_txn.setdefault(sr["transaction_id"], []).append(row_to_dict(sr))
            for t in txns:
                if t["id"] in splits_by_txn:
                    t["splits"] = splits_by_txn[t["id"]]

        return txns
    finally:
        conn.close()


@app.patch("/api/transactions/{transaction_id}")
def update_transaction(transaction_id: int, payload: TransactionUpdate):
    conn = get_connection()
    try:
        txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if not txn:
            raise HTTPException(404, "Transaction not found")

        conn.execute(
            "UPDATE transactions SET category_id = ? WHERE id = ?",
            (payload.category_id, transaction_id),
        )
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))

        if payload.apply_to_similar:
            _apply_correction_to_similar(conn, txn["account_id"], txn["raw_description"], payload.category_id)

        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.put("/api/transactions/{transaction_id}/splits")
def set_transaction_splits(transaction_id: int, payload: SplitsUpdate):
    conn = get_connection()
    try:
        txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        if not txn:
            raise HTTPException(404, "Transaction not found")
        if len(payload.splits) < 2:
            raise HTTPException(400, "A split needs at least two categories")

        total = sum(s.amount for s in payload.splits)
        if abs(total - txn["amount"]) > 0.01:
            raise HTTPException(
                400,
                f"Split amounts add up to {total:.2f}, but the transaction is {txn['amount']:.2f}",
            )

        conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))
        for s in payload.splits:
            conn.execute(
                "INSERT INTO transaction_splits (transaction_id, category_id, amount) VALUES (?, ?, ?)",
                (transaction_id, s.category_id, s.amount),
            )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/transactions/{transaction_id}/splits")
def clear_transaction_splits(transaction_id: int):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------- Statements & reconciliation ----------

def _iso_date(value: Optional[str], label: str) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, f"{label} isn't a valid date")


def _clean_statement(conn: sqlite3.Connection, payload: StatementSave) -> tuple:
    if not conn.execute("SELECT id FROM accounts WHERE id = ?", (payload.account_id,)).fetchone():
        raise HTTPException(404, "Account not found")
    period_end = _iso_date(payload.period_end, "Closing date")
    if period_end is None:
        raise HTTPException(400, "A statement needs a closing date")
    period_start = _iso_date(payload.period_start, "Start date")
    if period_start and period_start > period_end:
        raise HTTPException(400, "The start date is after the closing date")
    return payload.account_id, period_start, period_end, payload.previous_balance, payload.new_balance


@app.get("/api/statements")
def list_statements():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT s.*, a.name AS account_name, "
            "(SELECT COUNT(*) FROM transactions t WHERE t.statement_id = s.id) AS linked_count "
            "FROM statements s JOIN accounts a ON a.id = s.account_id "
            "ORDER BY a.name, s.period_end DESC"
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/statements")
def create_statement(payload: StatementSave):
    conn = get_connection()
    try:
        account_id, start, end, prev_balance, new_balance = _clean_statement(conn, payload)
        try:
            cur = conn.execute(
                "INSERT INTO statements (account_id, period_start, period_end, previous_balance, new_balance) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, start, end, prev_balance, new_balance),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(400, f"A statement closing {end} is already on file for this account -- edit it instead")
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@app.put("/api/statements/{statement_id}")
def update_statement(statement_id: int, payload: StatementSave):
    conn = get_connection()
    try:
        if not conn.execute("SELECT id FROM statements WHERE id = ?", (statement_id,)).fetchone():
            raise HTTPException(404, "Statement not found")
        account_id, start, end, prev_balance, new_balance = _clean_statement(conn, payload)
        try:
            conn.execute(
                "UPDATE statements SET account_id = ?, period_start = ?, period_end = ?, "
                "previous_balance = ?, new_balance = ? WHERE id = ?",
                (account_id, start, end, prev_balance, new_balance, statement_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(400, f"A statement closing {end} is already on file for this account")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/statements/{statement_id}")
def delete_statement(statement_id: int):
    conn = get_connection()
    try:
        # transactions.statement_id is ON DELETE SET NULL, so the transactions themselves are untouched.
        conn.execute("DELETE FROM statements WHERE id = ?", (statement_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def _link_transactions_to_statement(conn: sqlite3.Connection, statement_id: int, account_id: int,
                                    hashes: list[str]) -> set[str]:
    """Marks every already-saved transaction matching one of these dedupe hashes as belonging to the
    statement, and returns which of the hashes were found."""
    found: set[str] = set()
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        found.update(
            r["dedupe_hash"] for r in conn.execute(
                f"SELECT dedupe_hash FROM transactions WHERE account_id = ? AND dedupe_hash IN ({placeholders})",
                (account_id, *chunk),
            )
        )
        conn.execute(
            f"UPDATE transactions SET statement_id = ? WHERE account_id = ? AND dedupe_hash IN ({placeholders})",
            (statement_id, account_id, *chunk),
        )
    return found


def _record_statement_from_pdf(conn: sqlite3.Connection, account_id: int, filename: str, info: dict,
                               hashes: Optional[list[str]]) -> tuple[dict, set[str]]:
    """Saves the statement read from a PDF (unless one is already on file for that closing date --
    what's on file is never overwritten) and ties the PDF's transaction lines to it. `hashes` is None
    when the PDF's lines couldn't be parsed. Returns (result for the UI, hashes found in the database)."""
    if info["period_end"] is None or info["new_balance"] is None:
        return {**info, "status": "not_found"}, set()

    existing = conn.execute(
        "SELECT id FROM statements WHERE account_id = ? AND period_end = ?", (account_id, info["period_end"])
    ).fetchone()
    if existing:
        statement_id, status = existing["id"], "already_on_file"
    else:
        cur = conn.execute(
            "INSERT INTO statements (account_id, period_start, period_end, previous_balance, new_balance, source_file) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, info["period_start"], info["period_end"], info["previous_balance"],
             info["new_balance"], filename),
        )
        statement_id, status = cur.lastrowid, "saved"

    result = {**info, "status": status, "statement_id": statement_id}
    found: set[str] = set()
    if hashes is not None:
        found = _link_transactions_to_statement(conn, statement_id, account_id, hashes)
        conn.execute("UPDATE statements SET pdf_lines = ? WHERE id = ?", (len(hashes), statement_id))
        result["lines_in_pdf"] = len(hashes)
        result["lines_in_database"] = len(found)
    return result, found


@app.post("/api/statements/from_pdf")
async def statement_from_pdf(file: UploadFile = File(...), account_id: int = Form(...)):
    """Reads a statement PDF's period and balances (without importing its transactions) and, when
    those transactions are already in the database, ties them to the statement."""
    conn = get_connection()
    try:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            raise HTTPException(404, "Account not found")
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "That isn't a PDF")

        file_bytes = await file.read()
        info = extract_statement_info(file_bytes)
        if info["period_end"] is None or info["new_balance"] is None:
            raise HTTPException(
                400, "Couldn't find the closing date and new balance in this PDF -- enter the statement by hand instead."
            )

        parsed: list[NormalizedTransaction] = []
        note = None
        stored_mapping = json.loads(account["column_mapping"]) if account["column_mapping"] else None
        try:
            status, data = _parse_file(file.filename, file_bytes, None, account["institution"], stored_mapping)
            if status == "parsed":
                parsed = data["transactions"]
            else:
                note = "Couldn't match this PDF's transaction lines automatically, so only its totals were read."
        except ImportError_ as exc:
            note = str(exc)

        hashes = [make_dedupe_hash(account_id, t) for t in parsed] if parsed else None
        result, found = _record_statement_from_pdf(conn, account_id, file.filename, info, hashes)
        conn.commit()

        if hashes is not None:
            result["lines_identical"] = len(hashes) - len(set(hashes))
            result["missing"] = [
                {"date": t.date, "description": t.description, "amount": t.amount}
                for t, h in zip(parsed, hashes) if h not in found
            ][:10]
        if note:
            result["note"] = note
        return result
    finally:
        conn.close()


def _resolve_statement(conn: sqlite3.Connection, s: sqlite3.Row) -> tuple[Optional[str], Optional[float]]:
    """The statement's start date and previous balance -- as recorded, else continuing on from the
    previous statement on file for the same account."""
    prior = conn.execute(
        "SELECT period_end, new_balance FROM statements WHERE account_id = ? AND period_end < ? "
        "ORDER BY period_end DESC LIMIT 1",
        (s["account_id"], s["period_end"]),
    ).fetchone()
    start = s["period_start"]
    if start is None and prior:
        start = (date.fromisoformat(prior["period_end"]) + timedelta(days=1)).isoformat()
    prev_balance = s["previous_balance"]
    if prev_balance is None and prior:
        prev_balance = prior["new_balance"]
    return start, prev_balance


def _statement_members(conn: sqlite3.Connection, s: sqlite3.Row, start: Optional[str]) -> list[dict]:
    """The transactions counted toward a statement: the ones its PDF listed, plus any transaction not
    claimed by some other statement's PDF whose posting date (transaction date when there isn't one)
    falls in the statement period. Each transaction lands in at most one statement, so a purchase
    near a cycle edge is never counted twice."""
    members = [
        {**row_to_dict(r), "source": "statement"} for r in conn.execute(
            "SELECT id, date, description, amount FROM transactions WHERE statement_id = ?", (s["id"],)
        )
    ]
    if start is not None:
        members += [
            {**row_to_dict(r), "source": "date"} for r in conn.execute(
                "SELECT id, date, description, amount FROM transactions "
                "WHERE account_id = ? AND statement_id IS NULL AND COALESCE(posted_date, date) BETWEEN ? AND ?",
                (s["account_id"], start, s["period_end"]),
            )
        ]
    members.sort(key=lambda m: (m["date"], m["id"]))
    return members


def fmtusd(amount: float) -> str:
    return f"-${abs(amount):.2f}" if amount < 0 else f"${amount:.2f}"


def _reconcile_statement(conn: sqlite3.Connection, s: sqlite3.Row, account: sqlite3.Row) -> dict:
    start, prev_balance = _resolve_statement(conn, s)
    members = _statement_members(conn, s, start)
    linked = sum(1 for m in members if m["source"] == "statement")

    row = {
        "statement_id": s["id"], "account_id": s["account_id"], "account_name": account["name"],
        "period_start": start, "period_end": s["period_end"],
        "previous_balance": prev_balance, "new_balance": s["new_balance"],
        "source_file": s["source_file"], "txn_count": len(members), "linked_count": linked,
        "statement_change": None, "imported_change": None, "difference": None,
        "status": "incomplete", "notes": [], "possible_duplicates": [],
    }

    if prev_balance is None:
        row["notes"].append(
            "No previous balance to compare from -- add one to this statement, or add the statement before it.")
        return row
    if start is None and linked == 0:
        row["notes"].append(
            "No start date -- add one to this statement (or add the statement before it) so the period is known.")
        return row
    if start is None:
        row["notes"].append(
            "No start date on file, so only the transactions read from the statement PDF were counted.")

    # A card's balance is what's owed, so spending raises it; a bank balance moves with the money itself.
    is_card = account["account_type"] == "credit"
    sign = -1 if is_card else 1
    statement_change = round(s["new_balance"] - prev_balance, 2)
    imported_change = round(sign * sum(m["amount"] for m in members), 2)
    difference = round(imported_change - statement_change, 2)
    row.update(statement_change=statement_change, imported_change=imported_change, difference=difference,
               status="match" if difference == 0 else "mismatch")
    if difference == 0:
        return row

    gap = f"${abs(difference):.2f}"
    from_date_only = [m for m in members if m["source"] == "date"]
    pdf_change = round(sign * sum(m["amount"] for m in members if m["source"] == "statement"), 2)
    if linked and from_date_only and abs(pdf_change - statement_change) < 0.005:
        row["notes"].append(
            f"The {linked} transactions read from the statement PDF add up exactly. The {gap} difference comes from "
            f"{len(from_date_only)} other transaction(s) dated in this period that aren't on the PDF -- either a "
            "duplicate from another import, or a purchase that posts on the next statement (which clears once "
            "that statement is read).")
    elif is_card:
        row["notes"].append(
            f"Imported charges are {gap} higher than the statement -- look for a duplicated charge or a missing payment/credit."
            if difference > 0 else
            f"Imported charges are {gap} lower than the statement -- look for a missing charge or a duplicated payment/credit.")
    else:
        row["notes"].append(
            f"Imported deposits are {gap} higher than the statement -- look for a duplicated deposit or a missing withdrawal."
            if difference > 0 else
            f"Imported deposits are {gap} lower than the statement -- look for a missing deposit or a duplicated withdrawal.")

    if s["pdf_lines"] is not None and linked < s["pdf_lines"]:
        row["notes"].append(
            f"The statement PDF lists {s['pdf_lines']} transactions but only {linked} are in the database "
            "(a line is stored once if another line has the same date, description and amount).")

    # An extra transaction shows up as a difference equal to its own effect on the balance.
    row["possible_duplicates"] = [
        {"date": m["date"], "description": m["description"], "amount": m["amount"]}
        for m in members if abs(sign * m["amount"] - difference) < 0.005
    ][:3]

    # A payment stored as a charge (or the reverse) is off by twice its amount.
    for m in members:
        if abs(2 * sign * m["amount"] - difference) < 0.005:
            row["notes"].append(
                f"{m['date']} {m['description']} ({fmtusd(m['amount'])}) may have the wrong sign -- a payment "
                "recorded as a charge, or the reverse. Flipping it would account for the whole difference.")
            break
    return row


@app.post("/api/reconcile/run")
def run_reconcile(payload: ReconcileRequest):
    """Compares each statement on file with the transactions imported for its period. Only ever runs
    when asked -- nothing calls this on a schedule."""
    conn = get_connection()
    try:
        accounts = {a["id"]: a for a in conn.execute("SELECT * FROM accounts")}
        query = "SELECT * FROM statements"
        params: tuple = ()
        if payload.account_id is not None:
            query += " WHERE account_id = ?"
            params = (payload.account_id,)
        statements = conn.execute(query, params).fetchall()

        results = [_reconcile_statement(conn, s, accounts[s["account_id"]]) for s in statements]
        results.sort(key=lambda r: r["period_end"], reverse=True)
        results.sort(key=lambda r: r["account_name"].lower())
        return {
            "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "matched": sum(1 for r in results if r["status"] == "match"),
            "flagged": sum(1 for r in results if r["status"] == "mismatch"),
            "incomplete": sum(1 for r in results if r["status"] == "incomplete"),
            "statements": results,
        }
    finally:
        conn.close()


@app.get("/api/statements/{statement_id}/transactions")
def statement_transactions(statement_id: int):
    """The exact transactions the reconciliation counted for a statement, and why each one counted."""
    conn = get_connection()
    try:
        s = conn.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
        if not s:
            raise HTTPException(404, "Statement not found")
        start, _ = _resolve_statement(conn, s)
        return {"period_start": start, "period_end": s["period_end"],
                "transactions": _statement_members(conn, s, start)}
    finally:
        conn.close()


# ---------- Summaries ----------

@app.get("/api/summary/month")
def summary_month(year: int, month: int):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT v.category_id, c.name, c.color, c.is_transfer, SUM(v.amount) AS total, COUNT(*) AS cnt "
            "FROM transaction_category_amounts v LEFT JOIN categories c ON c.id = v.category_id "
            "WHERE strftime('%Y', v.date) = ? AND strftime('%m', v.date) = ? "
            "GROUP BY v.category_id ORDER BY total ASC",
            (f"{year:04d}", f"{month:02d}"),
        ).fetchall()

        by_category = [
            {"category_id": r["category_id"], "name": r["name"], "color": r["color"],
             "is_transfer": bool(r["is_transfer"]), "amount": round(r["total"], 2), "count": r["cnt"]}
            for r in rows
        ]
        spend_total = round(sum(c["amount"] for c in by_category if c["amount"] < 0 and not c["is_transfer"]), 2)
        income_total = round(sum(c["amount"] for c in by_category if c["amount"] > 0 and not c["is_transfer"]), 2)
        transfers_total = round(sum(c["amount"] for c in by_category if c["is_transfer"]), 2)

        return {
            "year": year, "month": month,
            "spend_total": spend_total,
            "income_total": income_total,
            "transfers_total": transfers_total,
            "by_category": by_category,
        }
    finally:
        conn.close()


@app.get("/api/summary/year")
def summary_year(year: int):
    conn = get_connection()
    try:
        month_rows = conn.execute(
            "SELECT strftime('%m', v.date) AS month, v.category_id, c.name, c.color, c.is_transfer, "
            "SUM(v.amount) AS total "
            "FROM transaction_category_amounts v LEFT JOIN categories c ON c.id = v.category_id "
            "WHERE strftime('%Y', v.date) = ? "
            "GROUP BY month, v.category_id",
            (f"{year:04d}",),
        ).fetchall()

        cat_rows = conn.execute(
            "SELECT v.category_id, c.name, c.color, c.is_transfer, SUM(v.amount) AS total, COUNT(*) AS cnt "
            "FROM transaction_category_amounts v LEFT JOIN categories c ON c.id = v.category_id "
            "WHERE strftime('%Y', v.date) = ? "
            "GROUP BY v.category_id ORDER BY total ASC",
            (f"{year:04d}",),
        ).fetchall()

        by_month = [
            {"month": r["month"], "category_id": r["category_id"], "name": r["name"],
             "color": r["color"], "is_transfer": bool(r["is_transfer"]), "amount": round(r["total"], 2)}
            for r in month_rows
        ]
        by_category = [
            {"category_id": r["category_id"], "name": r["name"], "color": r["color"],
             "is_transfer": bool(r["is_transfer"]), "amount": round(r["total"], 2), "count": r["cnt"]}
            for r in cat_rows
        ]
        spend_total = round(sum(c["amount"] for c in by_category if c["amount"] < 0 and not c["is_transfer"]), 2)
        income_total = round(sum(c["amount"] for c in by_category if c["amount"] > 0 and not c["is_transfer"]), 2)
        transfers_total = round(sum(c["amount"] for c in by_category if c["is_transfer"]), 2)

        return {
            "year": year,
            "spend_total": spend_total,
            "income_total": income_total,
            "transfers_total": transfers_total,
            "by_month": by_month,
            "by_category": by_category,
        }
    finally:
        conn.close()


# ---------- Budget ----------

def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _trailing_12mo_bounds(year: int, month: int) -> tuple[str, str]:
    """The 12 calendar months immediately before (year, month) -- excludes the selected
    month itself so an in-progress month never skews its own suggestion."""
    end_year, end_month = _prev_month(year, month)
    start_year, start_month = end_year, end_month - 11
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    start = f"{start_year:04d}-{start_month:02d}-01"
    end_exclusive = f"{year:04d}-{month:02d}-01"
    return start, end_exclusive


@app.get("/api/budget/data")
def get_budget_data(year: int, month: int):
    conn = get_connection()
    try:
        trailing_start, trailing_end = _trailing_12mo_bounds(year, month)
        prev_year, prev_month = _prev_month(year, month)

        trailing_rows = conn.execute(
            "SELECT v.category_id, SUM(v.amount) AS total "
            "FROM transaction_category_amounts v JOIN categories c ON c.id = v.category_id "
            "WHERE v.date >= ? AND v.date < ? AND c.is_transfer = 0 "
            "GROUP BY v.category_id",
            (trailing_start, trailing_end),
        ).fetchall()
        trailing_totals = {r["category_id"]: abs(r["total"]) for r in trailing_rows}

        month_rows = conn.execute(
            "SELECT v.category_id, SUM(v.amount) AS total "
            "FROM transaction_category_amounts v JOIN categories c ON c.id = v.category_id "
            "WHERE strftime('%Y', v.date) = ? AND strftime('%m', v.date) = ? AND c.is_transfer = 0 "
            "GROUP BY v.category_id",
            (f"{year:04d}", f"{month:02d}"),
        ).fetchall()
        month_actuals = {r["category_id"]: abs(r["total"]) for r in month_rows}

        this_month_budgets = {
            r["category_id"]: r["amount"] for r in conn.execute(
                "SELECT category_id, amount FROM category_budgets WHERE year = ? AND month = ?", (year, month)
            ).fetchall()
        }
        prev_month_budgets = {
            r["category_id"]: r["amount"] for r in conn.execute(
                "SELECT category_id, amount FROM category_budgets WHERE year = ? AND month = ?",
                (prev_year, prev_month),
            ).fetchall()
        }

        categories = conn.execute(
            "SELECT c.*, g.name AS group_name FROM categories c "
            "LEFT JOIN category_groups g ON g.id = c.group_id "
            "WHERE c.is_transfer = 0 AND (g.name IS NULL OR g.name != 'Income') "
            "ORDER BY c.name"
        ).fetchall()

        results = []
        for c in categories:
            cid = c["id"]
            trailing_total = trailing_totals.get(cid, 0.0)
            frequency = c["budget_frequency"] or "monthly"

            if frequency == "annual":
                suggested = trailing_total
                actual = trailing_total
            else:
                suggested = trailing_total / 12
                actual = month_actuals.get(cid, 0.0)

            saved = this_month_budgets.get(cid)
            if saved is None:
                saved = prev_month_budgets.get(cid)
            if saved is None:
                saved = round(suggested, 2)

            entry = {
                "category_id": cid,
                "name": c["name"],
                "color": c["color"],
                "group_name": c["group_name"],
                "frequency": frequency,
                "suggested": round(suggested, 2),
                "saved_amount": round(saved, 2),
                "actual": round(actual, 2),
            }
            if frequency == "annual":
                entry["amortized_monthly"] = round(saved / 12, 2)
            results.append(entry)

        return {"year": year, "month": month, "categories": results}
    finally:
        conn.close()


@app.put("/api/budget/data")
def save_budget_data(payload: BudgetSaveRequest):
    conn = get_connection()
    try:
        for entry in payload.entries:
            conn.execute(
                "INSERT INTO category_budgets (category_id, year, month, amount, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(category_id, year, month) DO UPDATE SET "
                "amount = excluded.amount, updated_at = excluded.updated_at",
                (entry.category_id, payload.year, payload.month, entry.amount),
            )
        conn.commit()
        return {"ok": True, "saved": len(payload.entries)}
    finally:
        conn.close()


# ---------- Static frontend ----------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
