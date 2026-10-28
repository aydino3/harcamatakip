"""Expense tracker business logic and SQLite/PostgreSQL persistence."""
from __future__ import annotations

import hashlib
import hmac
import base64
import csv
import calendar
import io
import os
import secrets
import re
import sqlite3
import threading
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# An explicit application URL takes priority during database migrations.
# Marketplace POSTGRES_URL remains the pooler-compatible default; DATABASE_URL
# is the final PostgreSQL fallback before local SQLite development storage.
def configured_database_url() -> str | None:
    return (
        os.environ.get("EXPENSE_DATABASE_URL", "").strip()
        or os.environ.get("POSTGRES_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
        or None
    )


_DATABASE_URL: str | None = configured_database_url()

if _DATABASE_URL:
    try:
        import psycopg2
        import psycopg2.errors
        _DB_INTEGRITY_ERRORS = (psycopg2.IntegrityError,)
    except ImportError:
        psycopg2 = None  # type: ignore
        _DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
else:
    _DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

ROOT = Path(__file__).resolve().parent


def _default_db_path() -> Path:
    if os.environ.get("VERCEL", "").strip().lower() == "1":
        return Path("/tmp/expense-tracker.sqlite3")
    return ROOT / "data" / "expenses.sqlite3"


def _default_backup_dir() -> Path:
    if os.environ.get("VERCEL", "").strip().lower() == "1":
        return Path("/tmp/expense-tracker-backups")
    return _default_db_path().parent / "backups"


if os.environ.get("VERCEL", "").strip().lower() == "1":
    os.environ.setdefault("APP_ENV", "production")
    os.environ.setdefault("PRIVATE_BETA", "0")
    os.environ.setdefault("COOKIE_SECURE", "1")
    os.environ.setdefault("TRUST_PROXY", "1")
    os.environ.setdefault("EXPENSE_DB_PATH", "/tmp/expense-tracker.sqlite3")
    os.environ.setdefault("EXPENSE_BACKUP_DIR", "/tmp/expense-tracker-backups")

DB_PATH = Path(os.environ.get("EXPENSE_DB_PATH", str(_default_db_path())))
BACKUP_DIR = Path(os.environ.get("EXPENSE_BACKUP_DIR", str(_default_backup_dir())))
COOKIE = "expense_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
LOCK = threading.Lock()
MAX_MINOR = 1_000_000_000_000_000  # 10 trillion TRY; safely representable by the browser UI
MAX_CSV_BYTES = 1_000_000
ENCRYPTED_BACKUP_MAGIC = b"EXPBKUP1"
ENCRYPTED_BACKUP_AAD = b"expense-tracker-sqlite-backup-v1"
CSV_HEADERS = ["Tarih", "Tutar", "Açıklama", "Kategori", "Ödeme yöntemi", "Not"]

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash BLOB NOT NULL, password_salt BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS categories (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id, name), UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS payment_methods (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, UNIQUE(user_id,name), UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS expenses (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category_id INTEGER, payment_method_id INTEGER, card_id INTEGER, statement_id INTEGER, installments_count INTEGER NOT NULL DEFAULT 1, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 currency TEXT NOT NULL DEFAULT 'TRY' CHECK(currency='TRY'), spent_on TEXT NOT NULL, description TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(category_id,user_id) REFERENCES categories(id,user_id),
 FOREIGN KEY(payment_method_id,user_id) REFERENCES payment_methods(id,user_id)
);
CREATE TABLE IF NOT EXISTS credit_cards (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 bank_name TEXT NOT NULL, display_name TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '', last_four TEXT NOT NULL DEFAULT '',
 closing_day INTEGER NOT NULL CHECK(closing_day BETWEEN 1 AND 31), due_day INTEGER NOT NULL CHECK(due_day BETWEEN 1 AND 31),
 due_days_after_close INTEGER NOT NULL DEFAULT 20 CHECK(due_days_after_close BETWEEN 1 AND 90),
 credit_limit_minor INTEGER CHECK(credit_limit_minor IS NULL OR credit_limit_minor>0), opening_balance_minor INTEGER NOT NULL DEFAULT 0 CHECK(opening_balance_minor>=0),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS card_statements (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, card_id INTEGER NOT NULL, period_start TEXT NOT NULL, closing_on TEXT NOT NULL, due_on TEXT NOT NULL,
 dates_manually_corrected INTEGER NOT NULL DEFAULT 0 CHECK(dates_manually_corrected IN (0,1)),
 UNIQUE(card_id,period_start), FOREIGN KEY(card_id,user_id) REFERENCES credit_cards(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS card_repayments (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, card_id INTEGER NOT NULL, statement_id INTEGER, amount_minor INTEGER NOT NULL CHECK(amount_minor>0), paid_on TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(card_id,user_id) REFERENCES credit_cards(id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(statement_id) REFERENCES card_statements(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS installment_plans (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, expense_id INTEGER NOT NULL UNIQUE, card_id INTEGER NOT NULL, original_minor INTEGER NOT NULL CHECK(original_minor>0), count INTEGER NOT NULL CHECK(count BETWEEN 2 AND 120), first_on TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(expense_id) REFERENCES expenses(id) ON DELETE CASCADE,
 FOREIGN KEY(card_id,user_id) REFERENCES credit_cards(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS installment_occurrences (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, due_on TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0), status TEXT NOT NULL DEFAULT 'unpaid' CHECK(status IN ('unpaid','paid','cancelled')), paid_on TEXT, statement_id INTEGER,
 UNIQUE(plan_id,ordinal), FOREIGN KEY(plan_id) REFERENCES installment_plans(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS card_statements_user_card ON card_statements(user_id,card_id,closing_on);
CREATE INDEX IF NOT EXISTS card_repayments_user_card ON card_repayments(user_id,card_id,paid_on);
CREATE INDEX IF NOT EXISTS expenses_user_date ON expenses(user_id,spent_on);
CREATE TABLE IF NOT EXISTS budgets (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 month TEXT NOT NULL, category_id INTEGER, amount_minor INTEGER NOT NULL CHECK(amount_minor>=0),
 UNIQUE(user_id,month,category_id),
 FOREIGN KEY(category_id,user_id) REFERENCES categories(id,user_id)
);
CREATE TABLE IF NOT EXISTS recurring_expenses (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category_id INTEGER, payment_method_id INTEGER, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 description TEXT NOT NULL, frequency TEXT NOT NULL CHECK(frequency IN ('weekly','monthly','yearly')),
 start_on TEXT NOT NULL, end_on TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','cancelled')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,user_id),
 FOREIGN KEY(category_id,user_id) REFERENCES categories(id,user_id),
 FOREIGN KEY(payment_method_id,user_id) REFERENCES payment_methods(id,user_id)
);
CREATE INDEX IF NOT EXISTS recurring_user_status ON recurring_expenses(user_id,status);
CREATE TABLE IF NOT EXISTS recurring_occurrences (
 id INTEGER PRIMARY KEY, recurring_id INTEGER NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 scheduled_on TEXT NOT NULL, expense_id INTEGER REFERENCES expenses(id) ON DELETE SET NULL,
 paid_on TEXT NOT NULL, UNIQUE(recurring_id,scheduled_on),
 FOREIGN KEY(recurring_id,user_id) REFERENCES recurring_expenses(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS auth_sessions_v2 (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_failures (
 source_hash TEXT NOT NULL, scope TEXT NOT NULL, occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS auth_failures_window ON auth_failures(source_hash,scope,occurred_at);
CREATE TABLE IF NOT EXISTS invitations (
 id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL,
 used_at TEXT, revoked_at TEXT
);
-- Additive migration: income records remain separate from expenses so expected
-- income can never inflate expense budgets or confirmed cash totals.
CREATE TABLE IF NOT EXISTS income_rules (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category_id INTEGER,
 income_type TEXT NOT NULL CHECK(income_type IN ('salary','additional','bonus','freelance','refund','other')),
 description TEXT NOT NULL, frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly','yearly')),
 schedule_mode TEXT NOT NULL DEFAULT 'anchored' CHECK(schedule_mode IN ('anchored','last_day')),
 start_on TEXT NOT NULL, end_on TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','cancelled')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS income_rule_amounts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, rule_id INTEGER NOT NULL,
 effective_on TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 UNIQUE(rule_id,effective_on),
 FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS income_records (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 rule_id INTEGER, category_id INTEGER, income_type TEXT NOT NULL CHECK(income_type IN ('salary','additional','bonus','freelance','refund','other')),
 description TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0), currency TEXT NOT NULL DEFAULT 'TRY' CHECK(currency='TRY'),
 expected_on TEXT NOT NULL, received_on TEXT, status TEXT NOT NULL DEFAULT 'expected' CHECK(status IN ('expected','received','cancelled')),
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(rule_id,expected_on), FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS income_records_user_date ON income_records(user_id,expected_on);
CREATE INDEX IF NOT EXISTS income_rules_user_status ON income_rules(user_id,status);
-- Additive cash-planning ledger. Existing transactions are never rewritten.
CREATE TABLE IF NOT EXISTS cash_settings (
 user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 opening_balance_minor INTEGER NOT NULL CHECK(opening_balance_minor>=0),
 effective_on TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cash_adjustments (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 adjustment_on TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor!=0),
 description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 160),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,user_id)
);
CREATE INDEX IF NOT EXISTS cash_adjustments_user_date ON cash_adjustments(user_id,adjustment_on,id);
"""

DEFAULT_CATEGORIES = ["Yemek", "Market", "Ulaşım", "Ev", "Sağlık", "Eğlence", "Diğer"]
DEFAULT_METHODS = ["Nakit", "Banka kartı", "Kredi kartı"]


def connect(allow_create=False):
    """Return PostgreSQL when configured, else the local SQLite database."""
    global _DATABASE_URL
    url = _DATABASE_URL or configured_database_url()
    if url:
        from pg_compat import connect_pg
        return connect_pg(url)
    # ---------- SQLite path (local development) ----------
    if allow_create:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt" and DB_PATH.parent == ROOT / "data":
            os.chmod(DB_PATH.parent, 0o700)
        db = sqlite3.connect(DB_PATH)
    else:
        if DB_PATH.is_symlink():
            raise sqlite3.OperationalError("Database path must not be a symbolic link.")
        db = sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=rw", uri=True)
    if os.name != "nt":
        os.chmod(DB_PATH, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    url = configured_database_url()
    if url:
        # PostgreSQL: apply schema directly (idempotent CREATE TABLE IF NOT EXISTS)
        from pg_compat import connect_pg
        with connect_pg(url) as db:
            _apply_pg_schema(db)
            _initialize_usernames(db)
        return
    # SQLite path
    with LOCK, connect(allow_create=True) as db:
        _apply_additive_schema(db)
        _initialize_usernames(db)
        db.execute("DELETE FROM budgets WHERE category_id IS NULL AND id NOT IN (SELECT MAX(id) FROM budgets WHERE category_id IS NULL GROUP BY user_id,month)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS budget_overall_unique ON budgets(user_id,month) WHERE category_id IS NULL")
    if os.environ.get("APP_ENV", "development").strip().lower() == "production":
        marker = DB_PATH.with_name(f".{DB_PATH.name}.initialized")
        if marker.is_symlink():
            raise ValueError("Production database initialization marker must not be a symbolic link.")
        if not marker.exists():
            try:
                descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as marker_file:
                    marker_file.write(b"expense-db-initialized-v1\n")
                    marker_file.flush()
                    os.fsync(marker_file.fileno())
            except FileExistsError:
                pass


def validate_runtime_configuration():
    """Require an explicit, writable absolute SQLite path in production, with a Vercel fallback for serverless hosting."""
    global DB_PATH, BACKUP_DIR
    # In PostgreSQL mode the connection string carries all configuration;
    # SQLite file-path and mount checks are not applicable.
    if configured_database_url():
        return True
    environment = os.environ.get("APP_ENV", "development").strip().lower()
    if environment not in ("development", "production"):
        raise ValueError("APP_ENV must be either development or production.")
    if environment != "production":
        return True

    if os.environ.get("VERCEL", "").strip().lower() == "1":
        raw_db_path = os.environ.get("EXPENSE_DB_PATH", str(_default_db_path())).strip()
        candidate = Path(raw_db_path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Vercel EXPENSE_DB_PATH must be an absolute path.")
        parent = candidate.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            try:
                os.chmod(parent, 0o700)
            except PermissionError:
                pass
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise ValueError("Production database path must be a regular file, not a symbolic link or directory.")
        if not candidate.exists():
            candidate.touch(mode=0o600)
        if os.name != "nt":
            try:
                os.chmod(candidate, 0o600)
            except PermissionError:
                pass
        raw_backup = os.environ.get("EXPENSE_BACKUP_DIR", str(_default_backup_dir())).strip()
        backup_path = Path(raw_backup).expanduser()
        if not backup_path.is_absolute():
            raise ValueError("Vercel EXPENSE_BACKUP_DIR must be an absolute path.")
        backup_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            try:
                os.chmod(backup_path, 0o700)
            except PermissionError:
                pass
        DB_PATH, BACKUP_DIR = candidate, backup_path
        private_beta_enabled(strict=True)
        return True

    raw_db_path = os.environ.get("EXPENSE_DB_PATH", "").strip()
    if not raw_db_path:
        raise ValueError("Production requires EXPENSE_DB_PATH to point inside a persistent disk mount.")
    candidate = Path(raw_db_path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Production EXPENSE_DB_PATH must be an absolute path.")
    raw_persistent_root = os.environ.get("PERSISTENT_DATA_DIR", "").strip()
    if not raw_persistent_root:
        raise ValueError("Production requires PERSISTENT_DATA_DIR for the mounted storage directory.")
    persistent_root = Path(raw_persistent_root).expanduser()
    if not persistent_root.is_absolute() or not persistent_root.exists() or not persistent_root.is_dir():
        raise ValueError("PERSISTENT_DATA_DIR must be an existing absolute mounted directory.")
    if os.environ.get("REQUIRE_PERSISTENT_MOUNT", "1") == "1" and not os.path.ismount(persistent_root):
        raise ValueError("PERSISTENT_DATA_DIR is not detected as a mounted filesystem; refusing to create the production database.")
    persistent_root = persistent_root.resolve()
    try:
        candidate.resolve().relative_to(persistent_root)
    except ValueError as error:
        raise ValueError("Production EXPENSE_DB_PATH must be inside PERSISTENT_DATA_DIR.") from error
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("Production database directory must already exist; mount the persistent disk before startup.")
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
        raise ValueError("Production database path must be a regular file, not a symbolic link or directory.")
    marker = parent / f".{candidate.name}.initialized"
    if marker.is_symlink():
        raise ValueError("Production database initialization marker must not be a symbolic link.")
    if marker.exists():
        if not candidate.is_file():
            raise ValueError("Previously initialized production database is missing; refusing to create an empty replacement.")
        try:
            validate_database(candidate, PRE_CARD_REQUIRED_SCHEMA)
        except ValueError as error:
            raise ValueError("Previously initialized production database is invalid; refusing to bootstrap over it.") from error
    if candidate.exists():
        try:
            descriptor = os.open(candidate, os.O_RDWR)
            os.close(descriptor)
        except OSError as error:
            raise ValueError("Production database file is not writable.") from error
    try:
        descriptor, probe = tempfile.mkstemp(prefix=".expense-write-check-", dir=parent)
        os.close(descriptor)
        Path(probe).unlink()
    except OSError as error:
        raise ValueError("Production database directory is not writable.") from error
    raw_backup = os.environ.get("EXPENSE_BACKUP_DIR", str(parent / "backups")).strip()
    backup_path = Path(raw_backup).expanduser()
    if not backup_path.is_absolute():
        raise ValueError("Production EXPENSE_BACKUP_DIR must be an absolute path.")
    resolved_backup = backup_path.resolve()
    try:
        resolved_backup.relative_to(persistent_root)
    except ValueError as error:
        raise ValueError("Production backups must be stored on the configured persistent disk.") from error
    _assert_outside_static(resolved_backup)
    DB_PATH, BACKUP_DIR = candidate, backup_path
    port = os.environ.get("PORT", "10000")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("PORT must be a valid TCP port number.")
    private_beta_enabled(strict=True)
    return True


def private_beta_enabled(strict=False):
    """Read the explicit private-beta switch; development defaults to open signup."""
    value = os.environ.get("PRIVATE_BETA")
    if value is None:
        if strict:
            raise ValueError("Production requires PRIVATE_BETA to be explicitly set to 0 or 1.")
        return False
    normalized = value.strip()
    if normalized not in ("0", "1"):
        raise ValueError("PRIVATE_BETA must be either 0 or 1.")
    return normalized == "1"


REQUIRED_SCHEMA = {
    "users": {"id", "email", "password_hash", "password_salt"},
    "categories": {"id", "user_id", "name", "archived"},
    "payment_methods": {"id", "user_id", "name"},
    "expenses": {"id", "user_id", "category_id", "payment_method_id", "card_id", "statement_id", "installments_count", "amount_minor", "spent_on", "description", "note"},
    "budgets": {"id", "user_id", "month", "category_id", "amount_minor"},
    "auth_sessions_v2": {"token_hash", "user_id", "csrf_token", "expires_at"},
    "recurring_expenses": {"id", "user_id", "category_id", "payment_method_id", "amount_minor", "description", "frequency", "start_on", "end_on", "status"},
    "recurring_occurrences": {"id", "recurring_id", "user_id", "scheduled_on", "expense_id", "paid_on"},
    "income_rules": {"id", "user_id", "category_id", "income_type", "description", "frequency", "schedule_mode", "start_on", "end_on", "status"},
    "income_rule_amounts": {"id", "user_id", "rule_id", "effective_on", "amount_minor"},
    "income_records": {"id", "user_id", "rule_id", "category_id", "income_type", "description", "amount_minor", "currency", "expected_on", "received_on", "status", "note"},
    "credit_cards": {"id", "user_id", "bank_name", "display_name", "closing_day", "due_day", "due_days_after_close", "credit_limit_minor", "opening_balance_minor", "active"},
    "card_statements": {"id", "user_id", "card_id", "period_start", "closing_on", "due_on", "dates_manually_corrected"},
    "card_repayments": {"id", "user_id", "card_id", "statement_id", "amount_minor", "paid_on"},
    "installment_plans": {"id", "user_id", "expense_id", "card_id", "original_minor", "count", "first_on"},
    "installment_occurrences": {"id", "user_id", "plan_id", "ordinal", "due_on", "amount_minor", "status", "statement_id"},
    "cash_settings": {"user_id", "opening_balance_minor", "effective_on"},
    "cash_adjustments": {"id", "user_id", "adjustment_on", "amount_minor", "description"},
}

# Schema supported immediately before card management. This lets production
# startup and restore safely upgrade existing databases in place or in staging.
PRE_CARD_REQUIRED_SCHEMA = {name: set(columns) for name, columns in REQUIRED_SCHEMA.items() if name not in {
    "credit_cards", "card_statements", "card_repayments", "installment_plans", "installment_occurrences"
}}
PRE_CARD_REQUIRED_SCHEMA["expenses"] -= {"card_id", "statement_id", "installments_count"}
PRE_CARD_REQUIRED_SCHEMA["income_rules"].discard("category_id")
PRE_CARD_REQUIRED_SCHEMA["income_records"].discard("category_id")


def _apply_additive_schema(db):
    db.executescript(SCHEMA)
    expense_columns = {row[1] for row in db.execute('PRAGMA table_info("expenses")')}
    if "card_id" not in expense_columns:
        db.execute("ALTER TABLE expenses ADD COLUMN card_id INTEGER")
    if "statement_id" not in expense_columns:
        db.execute("ALTER TABLE expenses ADD COLUMN statement_id INTEGER")
    if "installments_count" not in expense_columns:
        db.execute("ALTER TABLE expenses ADD COLUMN installments_count INTEGER NOT NULL DEFAULT 1")
    card_columns = {row[1] for row in db.execute('PRAGMA table_info("credit_cards")')}
    if "due_day" not in card_columns:
        db.execute("ALTER TABLE credit_cards ADD COLUMN due_day INTEGER NOT NULL DEFAULT 1 CHECK(due_day BETWEEN 1 AND 31)")
        today = date.today()
        cards = db.execute("SELECT id,closing_day,due_days_after_close FROM credit_cards").fetchall()
        for card in cards:
            closing = next_card_closing_date(int(card["closing_day"]), today)
            legacy_due = closing + timedelta(days=int(card["due_days_after_close"]))
            db.execute("UPDATE credit_cards SET due_day=? WHERE id=?", (legacy_due.day, card["id"]))
    statement_columns = {row[1] for row in db.execute('PRAGMA table_info("card_statements")')}
    if "dates_manually_corrected" not in statement_columns:
        db.execute("ALTER TABLE card_statements ADD COLUMN dates_manually_corrected INTEGER NOT NULL DEFAULT 1 CHECK(dates_manually_corrected IN (0,1))")
    for table in ("income_rules", "income_records"):
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if "category_id" not in columns:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN category_id INTEGER')
    _migrate_income_daily_frequency(db)


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE,
 password_hash BYTEA NOT NULL, password_salt BYTEA NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS categories (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id, name), UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS payment_methods (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, UNIQUE(user_id,name), UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS expenses (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category_id BIGINT, payment_method_id BIGINT, card_id BIGINT, statement_id BIGINT,
 installments_count INTEGER NOT NULL DEFAULT 1,
 amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 currency TEXT NOT NULL DEFAULT 'TRY' CHECK(currency='TRY'),
 spent_on TEXT NOT NULL, description TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(category_id,user_id) REFERENCES categories(id,user_id),
 FOREIGN KEY(payment_method_id,user_id) REFERENCES payment_methods(id,user_id)
);
CREATE TABLE IF NOT EXISTS credit_cards (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 bank_name TEXT NOT NULL, display_name TEXT NOT NULL, nickname TEXT NOT NULL DEFAULT '',
 last_four TEXT NOT NULL DEFAULT '', closing_day INTEGER NOT NULL CHECK(closing_day BETWEEN 1 AND 31),
 due_day INTEGER NOT NULL CHECK(due_day BETWEEN 1 AND 31),
 due_days_after_close INTEGER NOT NULL DEFAULT 20 CHECK(due_days_after_close BETWEEN 1 AND 90),
 credit_limit_minor BIGINT CHECK(credit_limit_minor IS NULL OR credit_limit_minor>0),
 opening_balance_minor BIGINT NOT NULL DEFAULT 0 CHECK(opening_balance_minor>=0),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS card_statements (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, card_id BIGINT NOT NULL,
 period_start TEXT NOT NULL, closing_on TEXT NOT NULL, due_on TEXT NOT NULL,
 dates_manually_corrected INTEGER NOT NULL DEFAULT 0 CHECK(dates_manually_corrected IN (0,1)),
 UNIQUE(card_id,period_start),
 FOREIGN KEY(card_id,user_id) REFERENCES credit_cards(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS card_repayments (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, card_id BIGINT NOT NULL,
 statement_id BIGINT, amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 paid_on TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(card_id,user_id) REFERENCES credit_cards(id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(statement_id) REFERENCES card_statements(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS installment_plans (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, expense_id BIGINT NOT NULL UNIQUE,
 card_id BIGINT NOT NULL, original_minor BIGINT NOT NULL CHECK(original_minor>0),
 count INTEGER NOT NULL CHECK(count BETWEEN 2 AND 120), first_on TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(expense_id) REFERENCES expenses(id) ON DELETE CASCADE,
 FOREIGN KEY(card_id,user_id) REFERENCES credit_cards(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS installment_occurrences (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, plan_id BIGINT NOT NULL,
 ordinal INTEGER NOT NULL, due_on TEXT NOT NULL,
 amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 status TEXT NOT NULL DEFAULT 'unpaid' CHECK(status IN ('unpaid','paid','cancelled')),
 paid_on TEXT, statement_id BIGINT,
 UNIQUE(plan_id,ordinal),
 FOREIGN KEY(plan_id) REFERENCES installment_plans(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS card_statements_user_card ON card_statements(user_id,card_id,closing_on);
CREATE INDEX IF NOT EXISTS card_repayments_user_card ON card_repayments(user_id,card_id,paid_on);
CREATE INDEX IF NOT EXISTS expenses_user_date ON expenses(user_id,spent_on);
CREATE TABLE IF NOT EXISTS budgets (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 month TEXT NOT NULL, category_id BIGINT, amount_minor BIGINT NOT NULL CHECK(amount_minor>=0),
 UNIQUE(user_id,month,category_id),
 FOREIGN KEY(category_id,user_id) REFERENCES categories(id,user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS budget_overall_unique ON budgets(user_id,month) WHERE category_id IS NULL;
CREATE TABLE IF NOT EXISTS recurring_expenses (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category_id BIGINT, payment_method_id BIGINT,
 amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 description TEXT NOT NULL,
 frequency TEXT NOT NULL CHECK(frequency IN ('weekly','monthly','yearly')),
 start_on TEXT NOT NULL, end_on TEXT,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','cancelled')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,user_id),
 FOREIGN KEY(category_id,user_id) REFERENCES categories(id,user_id),
 FOREIGN KEY(payment_method_id,user_id) REFERENCES payment_methods(id,user_id)
);
CREATE INDEX IF NOT EXISTS recurring_user_status ON recurring_expenses(user_id,status);
CREATE TABLE IF NOT EXISTS recurring_occurrences (
 id BIGSERIAL PRIMARY KEY, recurring_id BIGINT NOT NULL, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 scheduled_on TEXT NOT NULL, expense_id BIGINT REFERENCES expenses(id) ON DELETE SET NULL,
 paid_on TEXT NOT NULL, UNIQUE(recurring_id,scheduled_on),
 FOREIGN KEY(recurring_id,user_id) REFERENCES recurring_expenses(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_failures (
 source_hash TEXT NOT NULL, scope TEXT NOT NULL,
 occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS auth_failures_window ON auth_failures(source_hash,scope,occurred_at);
CREATE TABLE IF NOT EXISTS invitations (
 id BIGSERIAL PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TEXT NOT NULL,
 used_at TEXT, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS income_rules (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 category_id BIGINT,
 income_type TEXT NOT NULL CHECK(income_type IN ('salary','additional','bonus','freelance','refund','other')),
 description TEXT NOT NULL,
 frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly','yearly')),
 schedule_mode TEXT NOT NULL DEFAULT 'anchored' CHECK(schedule_mode IN ('anchored','last_day')),
 start_on TEXT NOT NULL, end_on TEXT,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','cancelled')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(id,user_id)
);
CREATE TABLE IF NOT EXISTS income_rule_amounts (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, rule_id BIGINT NOT NULL,
 effective_on TEXT NOT NULL, amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 UNIQUE(rule_id,effective_on),
 FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS income_records (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 rule_id BIGINT, category_id BIGINT,
 income_type TEXT NOT NULL CHECK(income_type IN ('salary','additional','bonus','freelance','refund','other')),
 description TEXT NOT NULL,
 amount_minor BIGINT NOT NULL CHECK(amount_minor>0),
 currency TEXT NOT NULL DEFAULT 'TRY' CHECK(currency='TRY'),
 expected_on TEXT NOT NULL, received_on TEXT,
 status TEXT NOT NULL DEFAULT 'expected' CHECK(status IN ('expected','received','cancelled')),
 note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(rule_id,expected_on),
 FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS income_records_user_date ON income_records(user_id,expected_on);
CREATE INDEX IF NOT EXISTS income_rules_user_status ON income_rules(user_id,status);
CREATE TABLE IF NOT EXISTS cash_settings (
 user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 opening_balance_minor BIGINT NOT NULL CHECK(opening_balance_minor>=0),
 effective_on TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cash_adjustments (
 id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 adjustment_on TEXT NOT NULL, amount_minor BIGINT NOT NULL CHECK(amount_minor!=0),
 description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 160),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,user_id)
);
CREATE INDEX IF NOT EXISTS cash_adjustments_user_date ON cash_adjustments(user_id,adjustment_on,id);
"""


def _apply_pg_schema(db):
    """Apply the PostgreSQL schema idempotently using CREATE TABLE IF NOT EXISTS."""
    from pg_compat import _split_script, _translate_sql
    statements = _split_script(PG_SCHEMA)
    for stmt in statements:
        translated = _translate_sql(stmt)
        if translated:
            try:
                db._conn.cursor().execute(translated)
                db._conn.commit()
            except Exception:
                db._conn.rollback()
    # Use a fresh auth-session relation so an old, partial sessions table cannot
    # break current registration or sign-in. Keep all SQL identifiers explicit.
    db._conn.cursor().execute(
        """CREATE TABLE IF NOT EXISTS auth_sessions_v2 (
            token_hash TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )"""
    )
    # Heal partially provisioned relations left by earlier startup failures.
    for statement in (
        "ALTER TABLE auth_sessions_v2 ADD COLUMN IF NOT EXISTS token_hash TEXT",
        "ALTER TABLE auth_sessions_v2 ADD COLUMN IF NOT EXISTS user_id BIGINT",
        "ALTER TABLE auth_sessions_v2 ADD COLUMN IF NOT EXISTS csrf_token TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE auth_sessions_v2 ADD COLUMN IF NOT EXISTS expires_at TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'",
    ):
        db._conn.cursor().execute(statement)
    db._conn.commit()


def _migrate_income_daily_frequency(db):
    """Add daily recurrence while preserving every income row and identifier."""
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='income_rules'").fetchone()[0]
    if "'daily'" in sql:
        return
    if db.in_transaction:
        db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE income_records RENAME TO income_records_before_daily")
        db.execute("ALTER TABLE income_rule_amounts RENAME TO income_rule_amounts_before_daily")
        db.execute("ALTER TABLE income_rules RENAME TO income_rules_before_daily")
        db.execute("""CREATE TABLE income_rules (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER, income_type TEXT NOT NULL CHECK(income_type IN ('salary','additional','bonus','freelance','refund','other')),
            description TEXT NOT NULL, frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly','yearly')),
            schedule_mode TEXT NOT NULL DEFAULT 'anchored' CHECK(schedule_mode IN ('anchored','last_day')),
            start_on TEXT NOT NULL, end_on TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','cancelled')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(id,user_id))""")
        db.execute("""CREATE TABLE income_rule_amounts (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, rule_id INTEGER NOT NULL,
            effective_on TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
            UNIQUE(rule_id,effective_on), FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE)""")
        db.execute("""CREATE TABLE income_records (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            rule_id INTEGER, category_id INTEGER, income_type TEXT NOT NULL CHECK(income_type IN ('salary','additional','bonus','freelance','refund','other')),
            description TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor>0), currency TEXT NOT NULL DEFAULT 'TRY' CHECK(currency='TRY'),
            expected_on TEXT NOT NULL, received_on TEXT, status TEXT NOT NULL DEFAULT 'expected' CHECK(status IN ('expected','received','cancelled')),
            note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(rule_id,expected_on), FOREIGN KEY(rule_id,user_id) REFERENCES income_rules(id,user_id) ON DELETE CASCADE)""")
        db.execute("INSERT INTO income_rules(id,user_id,category_id,income_type,description,frequency,schedule_mode,start_on,end_on,status,created_at,updated_at) SELECT id,user_id,category_id,income_type,description,frequency,schedule_mode,start_on,end_on,status,created_at,updated_at FROM income_rules_before_daily")
        db.execute("INSERT INTO income_rule_amounts(id,user_id,rule_id,effective_on,amount_minor) SELECT id,user_id,rule_id,effective_on,amount_minor FROM income_rule_amounts_before_daily")
        db.execute("INSERT INTO income_records(id,user_id,rule_id,category_id,income_type,description,amount_minor,currency,expected_on,received_on,status,note,created_at,updated_at) SELECT id,user_id,rule_id,category_id,income_type,description,amount_minor,currency,expected_on,received_on,status,note,created_at,updated_at FROM income_records_before_daily")
        db.execute("DROP TABLE income_records_before_daily")
        db.execute("DROP TABLE income_rule_amounts_before_daily")
        db.execute("DROP TABLE income_rules_before_daily")
        db.execute("CREATE INDEX IF NOT EXISTS income_records_user_date ON income_records(user_id,expected_on)")
        db.execute("CREATE INDEX IF NOT EXISTS income_rules_user_status ON income_rules(user_id,status)")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


def _assert_outside_static(path: Path):
    static_dir = (ROOT / "static").resolve()
    try:
        path.resolve().relative_to(static_dir)
    except ValueError:
        return
    raise ValueError("Backup storage must be outside the publicly served static directory.")


def _ensure_backup_dir():
    if BACKUP_DIR.is_symlink():
        raise ValueError("Backup directory must not be a symbolic link.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = BACKUP_DIR.resolve()
    _assert_outside_static(resolved)
    if not resolved.is_dir():
        raise ValueError("Backup location is not a directory.")
    if os.name != "nt":
        os.chmod(resolved, 0o700)
    return resolved


def _readonly_connection(path: Path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def validate_database(path: Path, required_schema=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Database file is missing or is a symbolic link.")
    _assert_outside_static(path)
    db = None
    try:
        db = _readonly_connection(path)
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise ValueError("Database integrity check failed.")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required_schema = REQUIRED_SCHEMA if required_schema is None else required_schema
        if set(required_schema) - tables:
            raise ValueError("Database schema is incompatible; required tables are missing.")
        for table, required_columns in required_schema.items():
            columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if required_columns - columns:
                raise ValueError("Database schema is incompatible; required columns are missing.")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Database contains foreign-key violations.")
    except sqlite3.DatabaseError as error:
        raise ValueError("Database file is invalid or corrupted.") from error
    finally:
        if db is not None:
            db.close()
    return True


def _copy_database_snapshot(source_path: Path, destination_path: Path):
    if not destination_path.exists():
        descriptor = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
    elif destination_path.is_symlink():
        raise ValueError("Temporary database path must not be a symbolic link.")
    if os.name != "nt":
        os.chmod(destination_path, 0o600)
    source = _readonly_connection(source_path)
    destination = None
    try:
        destination = sqlite3.connect(destination_path)
        source.backup(destination, pages=256, sleep=0.01)
        destination.commit()
    finally:
        if destination is not None:
            destination.close()
        source.close()
    if os.name != "nt":
        os.chmod(destination_path, 0o600)


def _private_temp_path(suffix: str):
    descriptor, name = tempfile.mkstemp(prefix="expense-private-", suffix=suffix)
    os.close(descriptor)
    return Path(name)


def _backup_encryption_key():
    encoded = os.environ.get("EXPENSE_BACKUP_KEY", "").strip()
    if not encoded:
        raise ValueError("EXPENSE_BACKUP_KEY must be set to create or restore an encrypted backup.")
    try:
        key = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("EXPENSE_BACKUP_KEY must be a URL-safe base64-encoded 32-byte key.") from error
    if len(key) != 32:
        raise ValueError("EXPENSE_BACKUP_KEY must be a URL-safe base64-encoded 32-byte key.")
    return key


def _encrypt_file(source: Path, destination: Path, key: bytes):
    nonce = secrets.token_bytes(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(ENCRYPTED_BACKUP_AAD)
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        output_file.write(ENCRYPTED_BACKUP_MAGIC + nonce)
        while chunk := input_file.read(1024 * 1024):
            output_file.write(encryptor.update(chunk))
        output_file.write(encryptor.finalize())
        output_file.write(encryptor.tag)
        output_file.flush()
        os.fsync(output_file.fileno())


def _decrypt_file(source: Path, destination: Path, key: bytes):
    minimum_size = len(ENCRYPTED_BACKUP_MAGIC) + 12 + 16
    size = source.stat().st_size
    if size < minimum_size:
        raise ValueError("Encrypted backup is truncated or invalid.")
    with source.open("rb") as input_file:
        header = input_file.read(len(ENCRYPTED_BACKUP_MAGIC) + 12)
        if len(header) != len(ENCRYPTED_BACKUP_MAGIC) + 12 or not header.startswith(ENCRYPTED_BACKUP_MAGIC):
            raise ValueError("Selected file is not a supported encrypted backup.")
        nonce = header[len(ENCRYPTED_BACKUP_MAGIC):]
        input_file.seek(size - 16)
        tag = input_file.read(16)
        input_file.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(ENCRYPTED_BACKUP_AAD)
        remaining = size - len(header) - 16
        with destination.open("wb") as output_file:
            if os.name != "nt":
                os.chmod(destination, 0o600)
            while remaining:
                chunk = input_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Encrypted backup is truncated or invalid.")
                remaining -= len(chunk)
                output_file.write(decryptor.update(chunk))
            try:
                output_file.write(decryptor.finalize())
            except Exception as error:
                raise ValueError("Encrypted backup authentication failed; check the key and file integrity.") from error
            output_file.flush()
            os.fsync(output_file.fileno())


def create_backup(encrypted=False):
    source_path = DB_PATH
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("Active database file does not exist or is a symbolic link.")
    validate_database(source_path)
    backup_dir = _ensure_backup_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if encrypted:
        key = _backup_encryption_key()
    filename = f"expenses-{stamp}-{secrets.token_hex(4)}.sqlite3" + (".enc" if encrypted else "")
    final_path = backup_dir / filename
    temporary_path = backup_dir / f".{filename}.tmp"
    snapshot_path = _private_temp_path(".sqlite3") if encrypted else temporary_path
    try:
        _copy_database_snapshot(source_path, snapshot_path)
        validate_database(snapshot_path)
        if encrypted:
            _encrypt_file(snapshot_path, temporary_path, key)
        os.replace(temporary_path, final_path)
        if os.name != "nt":
            os.chmod(final_path, 0o600)
        return final_path
    finally:
        temporary_path.unlink(missing_ok=True)
        if snapshot_path != temporary_path:
            snapshot_path.unlink(missing_ok=True)


def restore_backup(backup_path: str | Path):
    backup_dir = _ensure_backup_dir()
    candidate = Path(backup_path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("Selected backup file does not exist or is a symbolic link.")
    candidate = candidate.resolve()
    try:
        candidate.relative_to(backup_dir)
    except ValueError as error:
        raise ValueError("Restore accepts backup files only from the configured backup directory.") from error
    _assert_outside_static(candidate)
    active_path = DB_PATH
    if active_path.is_symlink():
        raise ValueError("Active database is a symbolic link; refusing to replace it.")
    if active_path.exists() and candidate == active_path.resolve():
        raise ValueError("Selected file is the active database, not a backup.")
    with candidate.open("rb") as backup_file:
        encrypted = backup_file.read(len(ENCRYPTED_BACKUP_MAGIC)) == ENCRYPTED_BACKUP_MAGIC
    if os.environ.get("APP_ENV", "development").strip().lower() == "production" and not encrypted:
        raise ValueError("Production restore requires an encrypted backup.")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(active_path) + suffix).exists():
            raise ValueError("SQLite sidecar files exist. Stop the app and ensure the database is closed before restore.")

    active_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = active_path.parent / f".{active_path.name}.restore-{secrets.token_hex(8)}.tmp"
    decrypted_path = _private_temp_path(".sqlite3") if encrypted else candidate
    migration_path = None
    try:
        if encrypted:
            _decrypt_file(candidate, decrypted_path, _backup_encryption_key())
        source_path = decrypted_path
        try:
            validate_database(source_path)
        except ValueError:
            # Accept the immediately previous schema only after integrity and
            # ownership constraints pass, then migrate a private staging copy.
            validate_database(source_path, PRE_CARD_REQUIRED_SCHEMA)
            migration_path = _private_temp_path(".sqlite3")
            _copy_database_snapshot(source_path, migration_path)
            with sqlite3.connect(migration_path) as staged_db:
                _apply_additive_schema(staged_db)
            validate_database(migration_path)
            source_path = migration_path
        recovery_backup = create_backup(encrypted=os.environ.get("APP_ENV", "development").strip().lower() == "production" or encrypted) if active_path.is_file() else None
        _copy_database_snapshot(source_path, temporary_path)
        validate_database(temporary_path)
        os.replace(temporary_path, active_path)
        return {"restored_from": candidate, "recovery_backup": recovery_backup}
    finally:
        temporary_path.unlink(missing_ok=True)
        if migration_path:
            migration_path.unlink(missing_ok=True)
        if encrypted:
            decrypted_path.unlink(missing_ok=True)


def password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)


DUMMY_PASSWORD_SALT = secrets.token_bytes(16)
DUMMY_PASSWORD_HASH = password_digest(secrets.token_urlsafe(32), DUMMY_PASSWORD_SALT)


def secure_cookie_enabled() -> bool:
    return COOKIE_SECURE or os.environ.get("APP_ENV", "development").strip().lower() == "production"


def month_bounds(month: str):
    try:
        if not isinstance(month, str) or len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
            raise ValueError
        y, m = map(int, month.split("-"))
        if not 1 <= m <= 12:
            raise ValueError
        start = date(y, m, 1)
        end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        return start.isoformat(), end.isoformat()
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid month")


class InvalidInvitation(ValueError):
    """Raised when a private-beta invitation is invalid or unavailable."""


def create_invitation(expires_hours=72):
    if isinstance(expires_hours, bool) or not isinstance(expires_hours, int) or not 1 <= expires_hours <= 720:
        raise ValueError("Invitation expiry must be between 1 and 720 hours.")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    with connect() as db:
        expires_at = (datetime.utcnow() + timedelta(hours=expires_hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = db.execute("INSERT INTO invitations(token_hash,expires_at) VALUES(?,?)",
                            (digest, expires_at))
        invitation_id = cursor.lastrowid
    return invitation_id, token


def revoke_invitation(invitation_id):
    if isinstance(invitation_id, bool) or not str(invitation_id).isdigit():
        return False
    with connect() as db:
        cursor = db.execute("UPDATE invitations SET revoked_at=CURRENT_TIMESTAMP WHERE id=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?",
                            (int(invitation_id), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    return cursor.rowcount == 1


class InvalidUsername(ValueError):
    pass


class UsernameUnavailable(ValueError):
    pass


def normalize_username(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,29}",value.strip()):
        raise InvalidUsername("Kullanıcı adı 3–30 karakter olmalı; İngilizce harf, rakam, nokta, tire ve alt çizgi kullanabilirsin.")
    return value.strip().lower()


def username_integrity_error(error):
    return 'users_username_unique' in str(error) or 'users.username' in str(error)


def _initialize_usernames(db):
    """Reserve existing visible names without renaming or deleting any account."""
    pg=hasattr(db,'_conn')
    if pg:
        db.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT')
        db.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT')
    else:
        columns={row[1] for row in db.execute('PRAGMA table_info(users)')}
        for field in ('username','display_name'):
            if field not in columns:db.execute(f'ALTER TABLE users ADD COLUMN {field} TEXT')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_username_unique ON users(lower(username)) WHERE username IS NOT NULL')
    users=[dict(row) for row in db.execute('SELECT id,email,display_name,username FROM users')]
    occupied={row['username'].lower() for row in users if row['username']}
    candidates={}
    for row in users:
        if row['username']:continue
        try:name=normalize_username(row['display_name'] or row['email'].split('@')[0])
        except InvalidUsername:continue
        candidates.setdefault(name,[]).append(row['id'])
    for name,ids in candidates.items():
        if name not in occupied:
            db.execute('UPDATE users SET username=? WHERE id=? AND username IS NULL',(name,ids[0]))


def create_user(email: str, password: str, invitation_token=None, require_invitation=None, username=None) -> int:
    if not isinstance(email, str) or not isinstance(password, str):
        raise ValueError("invalid credentials")
    email = email.strip().lower()
    if len(email) > 254 or email.count("@") != 1 or email.startswith("@") or email.endswith("@") or any(ch.isspace() for ch in email) or len(password) < 10 or len(password) > 256:
        raise ValueError("invalid credentials")
    configured_beta = private_beta_enabled(strict=os.environ.get("APP_ENV", "development").strip().lower() == "production")
    require_invitation = configured_beta or require_invitation is True
    if require_invitation and (not isinstance(invitation_token, str) or not 20 <= len(invitation_token) <= 256):
        raise InvalidInvitation("invalid invitation")
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest() if require_invitation else None
    username = normalize_username(username) if username is not None else None
    salt = secrets.token_bytes(16)
    with connect() as db:
        if require_invitation:
            db.execute("BEGIN IMMEDIATE")
            invitation = db.execute("SELECT id FROM invitations WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?", (token_hash, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
            if not invitation:
                raise InvalidInvitation("invalid invitation")
        if username is not None and db.execute('SELECT 1 FROM users WHERE lower(username)=?',(username,)).fetchone():
            raise UsernameUnavailable('Bu kullanıcı adı zaten kullanılıyor. Başka bir ad seç.')
        try:
            cur = db.execute("INSERT INTO users(email,password_hash,password_salt,username,display_name) VALUES(?,?,?,?,?)",
                             (email,password_digest(password,salt),salt,username,username))
        except _DB_INTEGRITY_ERRORS as error:
            if username_integrity_error(error):raise UsernameUnavailable('Bu kullanıcı adı zaten kullanılıyor. Başka bir ad seç.') from error
            raise
        user_id = cur.lastrowid
        db.executemany("INSERT INTO categories(user_id,name) VALUES(?,?)", [(user_id, n) for n in DEFAULT_CATEGORIES])
        db.executemany("INSERT INTO payment_methods(user_id,name) VALUES(?,?)", [(user_id, n) for n in DEFAULT_METHODS])
        if require_invitation:
            consumed = db.execute("UPDATE invitations SET used_at=CURRENT_TIMESTAMP WHERE id=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?", (invitation["id"], datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
            if consumed.rowcount != 1:
                raise InvalidInvitation("invalid invitation")
        return user_id


def authenticate(email: str, password: str):
    if not isinstance(email, str) or not isinstance(password, str):
        return None
    if len(email) > 254 or len(password) > 256:
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    salt = row["password_salt"] if row else DUMMY_PASSWORD_SALT
    expected = row["password_hash"] if row else DUMMY_PASSWORD_HASH
    matches = hmac.compare_digest(password_digest(password, salt), expected)
    if not row or not matches:
        return None
    return row["id"]


def create_session(user_id: int):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        expires_at = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("INSERT INTO auth_sessions_v2(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?) RETURNING token_hash",
                   (token_hash, user_id, csrf, expires_at))
    return token, csrf


def revoke_session(token: str | None):
    if not token:
        return
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        db.execute("DELETE FROM auth_sessions_v2 WHERE token_hash=?", (digest,))


def session_user(token: str | None):
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        row = db.execute("SELECT user_id,csrf_token FROM auth_sessions_v2 WHERE token_hash=? AND expires_at>?", (digest, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    return dict(row) if row else None


AUTH_FAILURE_LIMITS = {"login": 20, "register": 10, "account-delete": 10}
AUTH_SOURCE_LIMITS = {"login": 100, "register": 30, "account-delete": 50}
_AUTH_ATTEMPTS: dict[tuple[str, str], list[datetime]] = {}


def _auth_source_hash(address: str, scope: str, identity: str) -> str:
    normalized = str(identity or "").strip().casefold()
    return hashlib.sha256(f"{scope}\0{address}\0{normalized}".encode("utf-8", "replace")).hexdigest()


def reserve_auth_attempt(address: str, scope: str, identity: str = "") -> bool:
    """Atomically reserve an auth slot; true means this source/key is throttled."""
    limit = AUTH_SOURCE_LIMITS[scope] if not identity else AUTH_FAILURE_LIMITS[scope]
    source_hash = _auth_source_hash(address, scope, identity)
    if configured_database_url():
        # Serverless instances can cold-start concurrently, so avoid DDL and
        # writes to potentially legacy auth tables on the authentication path.
        # This per-instance guard remains bounded and never blocks account access
        # because of an unrelated database schema mismatch.
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=15)
        key = (source_hash, scope)
        with LOCK:
            attempts = [item for item in _AUTH_ATTEMPTS.get(key, []) if item >= cutoff]
            throttled = len(attempts) >= limit
            attempts.append(now)
            _AUTH_ATTEMPTS[key] = attempts
            if len(_AUTH_ATTEMPTS) > 5000:
                for old_key in list(_AUTH_ATTEMPTS)[:1000]:
                    kept = [item for item in _AUTH_ATTEMPTS[old_key] if item >= cutoff]
                    if kept:
                        _AUTH_ATTEMPTS[old_key] = kept
                    else:
                        _AUTH_ATTEMPTS.pop(old_key, None)
            return throttled
    with LOCK, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM auth_failures WHERE occurred_at<datetime('now','-1 day')")
        count = db.execute(
            "SELECT COUNT(*) FROM auth_failures WHERE source_hash=? AND scope=? AND occurred_at>=datetime('now','-15 minutes')",
            (source_hash, scope),
        ).fetchone()[0]
        if count >= limit:
            return True
        db.execute("INSERT INTO auth_failures(source_hash,scope) VALUES(?,?)", (source_hash, scope))
        return False

def clear_auth_failures(address: str, scope: str, identity: str = ""):
    source_hash = _auth_source_hash(address, scope, identity)
    if configured_database_url():
        with LOCK:
            _AUTH_ATTEMPTS.pop((source_hash, scope), None)
        return
    with connect() as db:
        db.execute("DELETE FROM auth_failures WHERE source_hash=? AND scope=?", (source_hash, scope))


def delete_account(user_id: int, password: str, confirmation: str) -> bool:
    if confirmation != "SİL" or not isinstance(password, str):
        return False
    with connect() as db:
        row = db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or authenticate(row["email"], password) != user_id:
        return False
    with LOCK, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return bool(cur.rowcount)


def summary(user_id: int, month: str, as_of: date | None = None):
    as_of = as_of or date.today()
    start, end = month_bounds(month)
    prev_y, prev_m = map(int, month.split("-"))
    prev_month = None if (prev_y == 1 and prev_m == 1) else (f"{prev_y-1:04d}-12" if prev_m == 1 else f"{prev_y:04d}-{prev_m-1:02d}")
    if prev_month:
        prev_start, prev_end = month_bounds(prev_month)
    with connect() as db:
        current_rows = db.execute("SELECT e.category_id,c.name,e.amount_minor FROM expenses e LEFT JOIN categories c ON c.id=e.category_id AND c.user_id=e.user_id WHERE e.user_id=? AND e.currency='TRY' AND e.spent_on>=? AND e.spent_on<?", (user_id,start,end)).fetchall()
        prior_rows = db.execute("SELECT amount_minor FROM expenses WHERE user_id=? AND currency='TRY' AND spent_on>=? AND spent_on<?", (user_id,prev_start,prev_end)).fetchall() if prev_month else []
        budget_row = db.execute("SELECT amount_minor FROM budgets WHERE user_id=? AND month=? AND category_id IS NULL ORDER BY id DESC LIMIT 1", (user_id,month)).fetchone()
        budget = budget_row["amount_minor"] if budget_row else None
        category_budgets = {r["category_id"]:r["amount_minor"] for r in db.execute("SELECT category_id,amount_minor FROM budgets WHERE user_id=? AND month=? AND category_id IS NOT NULL",(user_id,month)).fetchall()}
        recent = db.execute("SELECT e.id,e.amount_minor,e.currency,e.spent_on,e.description,c.name category,p.name payment_method FROM expenses e LEFT JOIN categories c ON c.id=e.category_id AND c.user_id=e.user_id LEFT JOIN payment_methods p ON p.id=e.payment_method_id AND p.user_id=e.user_id WHERE e.user_id=? AND e.currency='TRY' ORDER BY e.spent_on DESC,e.id DESC LIMIT 8", (user_id,)).fetchall()
    spent = sum(int(r["amount_minor"]) for r in current_rows)
    prior = sum(int(r["amount_minor"]) for r in prior_rows)
    grouped = {}
    for row in current_rows:
        category_id = row["category_id"]
        key = category_id
        if key not in grouped:
            grouped[key] = {"id":category_id,"name":row["name"] or "Kategorisiz","amount_minor":0,"budget_minor":category_budgets.get(category_id)}
        grouped[key]["amount_minor"] += int(row["amount_minor"])
    by_category = sorted(grouped.values(),key=lambda r:r["amount_minor"],reverse=True)
    for category in by_category:
        category["budget_percent"] = None if category["budget_minor"] is None else (100 if category["budget_minor"] == 0 and category["amount_minor"] else 0 if category["budget_minor"] == 0 else round(category["amount_minor"] * 100 / category["budget_minor"]))
        category["budget_status"] = None if category["budget_percent"] is None else "over" if category["budget_percent"] >= 100 else "near" if category["budget_percent"] >= 80 else "ok"
    forecast = None
    if month == as_of.strftime("%Y-%m") and as_of.day >= 5 and spent:
        days = calendar.monthrange(as_of.year, as_of.month)[1]
        forecast = (spent * days + as_of.day // 2) // as_of.day
    forecast_reason = "available" if forecast is not None else "not_current_month" if month != as_of.strftime("%Y-%m") else "insufficient_days" if as_of.day < 5 else "no_spending"
    income_overview(user_id, month, as_of)
    income = income_totals(user_id, month, as_of)
    return {"month":month,"spent_minor":spent,"budget_minor":budget,"remaining_minor":budget-spent if budget is not None else None,
            "percent":(100 if spent else 0) if budget == 0 else (round(spent*100/budget) if budget is not None else None),"previous_minor":prior,"change_minor":spent-prior,
            "categories":[dict(r) for r in by_category],"recent":[dict(r) for r in recent],
            "month_end_estimate_minor":forecast,"estimate_reason":forecast_reason,
            "income_received_minor":income["received_minor"],"income_expected_minor":income["expected_minor"]}


INCOME_TYPES = {"salary", "additional", "bonus", "freelance", "refund", "other"}


def income_recurrence_date(start_on: date, frequency: str, index: int, schedule_mode="anchored") -> date:
    """Return a stable scheduled date; salary rules may use calendar month-end."""
    if index < 0 or frequency not in ("daily", "weekly", "monthly", "yearly"):
        raise ValueError("invalid income recurrence")
    if frequency == "daily":
        return start_on + timedelta(days=index)
    if frequency == "weekly":
        return start_on + timedelta(days=7 * index)
    if frequency == "monthly":
        month_index = start_on.year * 12 + start_on.month - 1 + index
        year, month0 = divmod(month_index, 12)
        month = month0 + 1
        day = calendar.monthrange(year, month)[1] if schedule_mode == "last_day" else min(start_on.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    year = start_on.year + index
    return date(year, start_on.month, min(start_on.day, calendar.monthrange(year, start_on.month)[1]))


def income_recurrence_index(start_on: date, frequency: str, target: date, schedule_mode="anchored") -> int:
    if target <= start_on:
        return 0
    if frequency == "daily":
        return (target - start_on).days
    if frequency == "weekly":
        return (target - start_on).days // 7
    if frequency == "monthly":
        index = max(0, (target.year - start_on.year) * 12 + target.month - start_on.month)
    elif frequency == "yearly":
        index = max(0, target.year - start_on.year)
    else:
        raise ValueError("invalid income recurrence")
    try:
        return index if income_recurrence_date(start_on, frequency, index, schedule_mode) >= target else index + 1
    except (ValueError, OverflowError):
        return index


def _valid_income_fields(data):
    description = data.get("description")
    income_type = data.get("income_type", "additional")
    expected_value = data.get("expected_on") or data.get("start_on", "")
    note = data.get("note", "")
    if not all(isinstance(value, str) for value in (description, income_type, expected_value, note)):
        raise ValueError("invalid income fields")
    description = description.strip()
    expected_on = date.fromisoformat(expected_value)
    if expected_on.isoformat() != expected_value or not description or len(description) > 160 or len(note) > 1000 or income_type not in INCOME_TYPES:
        raise ValueError("invalid income")
    amount_minor = money_to_minor(data.get("amount"))
    if amount_minor <= 0 or amount_minor > MAX_MINOR:
        raise ValueError("invalid income amount")
    category_id = record_id(data.get("category_id"))
    return description, income_type, expected_on, note, amount_minor, category_id


def save_income(user_id: int, data: dict) -> int:
    """Create or edit a one-time expected income record."""
    description, income_type, expected_on, note, amount_minor, category_id = _valid_income_fields(data)
    income_id = record_id(data.get("id"))
    with connect() as db:
        if category_id and not db.execute("SELECT 1 FROM categories WHERE id=? AND user_id=? AND archived=0", (category_id, user_id)).fetchone():
            raise ValueError("invalid income category")
        if income_id:
            row = db.execute("SELECT status,rule_id FROM income_records WHERE id=? AND user_id=?", (income_id, user_id)).fetchone()
            if not row or row["rule_id"] is not None or row["status"] != "expected":
                return 0
            db.execute("UPDATE income_records SET category_id=?,income_type=?,description=?,amount_minor=?,expected_on=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                       (category_id, income_type, description, amount_minor, expected_on.isoformat(), note, income_id, user_id))
            return income_id
        cur = db.execute("INSERT INTO income_records(user_id,category_id,income_type,description,amount_minor,expected_on,note,status) VALUES(?,?,?,?,?,?,?, 'expected')",
                         (user_id, category_id, income_type, description, amount_minor, expected_on.isoformat(), note))
        return cur.lastrowid


def save_income_rule(user_id: int, data: dict) -> int:
    description, income_type, start_on, note, amount_minor, category_id = _valid_income_fields(data)
    end_value = data.get("end_on") or None
    if end_value is not None and not isinstance(end_value, str):
        raise ValueError("invalid end date")
    end_on = date.fromisoformat(end_value) if end_value else None
    if end_value and end_on.isoformat() != end_value or end_on and end_on < start_on:
        raise ValueError("invalid end date")
    frequency = data.get("frequency")
    if frequency not in ("daily", "weekly", "monthly", "yearly"):
        raise ValueError("invalid frequency")
    default_mode = "last_day" if income_type == "salary" and frequency == "monthly" else "anchored"
    schedule_mode = data.get("schedule_mode", default_mode)
    if schedule_mode not in ("anchored", "last_day") or schedule_mode == "last_day" and frequency != "monthly":
        raise ValueError("invalid schedule mode")
    if income_type == "salary" and frequency == "monthly" and "schedule_mode" not in data:
        schedule_mode = "last_day"
    rule_id = record_id(data.get("id"))
    with connect() as db:
        if category_id and not db.execute("SELECT 1 FROM categories WHERE id=? AND user_id=? AND archived=0", (category_id, user_id)).fetchone():
            raise ValueError("invalid income category")
        if rule_id:
            existing = db.execute("SELECT status FROM income_rules WHERE id=? AND user_id=?", (rule_id, user_id)).fetchone()
            if not existing or existing["status"] == "cancelled":
                return 0
            db.execute("UPDATE income_rules SET category_id=?,income_type=?,description=?,frequency=?,schedule_mode=?,start_on=?,end_on=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                       (category_id, income_type, description, frequency, schedule_mode, start_on.isoformat(), end_value, rule_id, user_id))
            effective_on = max(date.today(), start_on).isoformat()
            db.execute("INSERT INTO income_rule_amounts(user_id,rule_id,effective_on,amount_minor) VALUES(?,?,?,?) ON CONFLICT(rule_id,effective_on) DO UPDATE SET amount_minor=excluded.amount_minor",
                       (user_id, rule_id, effective_on, amount_minor))
            # Settings only affect future, unreceived projections. Historical or
            # already-confirmed records are deliberately retained unchanged.
            db.execute("DELETE FROM income_records WHERE user_id=? AND rule_id=? AND status='expected' AND expected_on>=?", (user_id, rule_id, effective_on))
            return rule_id
        cur = db.execute("INSERT INTO income_rules(user_id,category_id,income_type,description,frequency,schedule_mode,start_on,end_on) VALUES(?,?,?,?,?,?,?,?)",
                         (user_id, category_id, income_type, description, frequency, schedule_mode, start_on.isoformat(), end_value))
        rule_id = cur.lastrowid
        db.execute("INSERT INTO income_rule_amounts(user_id,rule_id,effective_on,amount_minor) VALUES(?,?,?,?)", (user_id, rule_id, start_on.isoformat(), amount_minor))
        return rule_id


def set_income_rule_amount(user_id: int, rule_id: int, effective_value: str, amount: str) -> bool:
    effective_on = date.fromisoformat(effective_value)
    if effective_on.isoformat() != effective_value or effective_on < date.today():
        raise ValueError("effective date must be today or later")
    amount_minor = money_to_minor(amount)
    if amount_minor <= 0 or amount_minor > MAX_MINOR:
        raise ValueError("invalid income amount")
    with connect() as db:
        rule = db.execute("SELECT id FROM income_rules WHERE id=? AND user_id=? AND status='active'", (rule_id, user_id)).fetchone()
        if not rule:
            return False
        db.execute("INSERT INTO income_rule_amounts(user_id,rule_id,effective_on,amount_minor) VALUES(?,?,?,?) ON CONFLICT(rule_id,effective_on) DO UPDATE SET amount_minor=excluded.amount_minor", (user_id, rule_id, effective_value, amount_minor))
        db.execute("DELETE FROM income_records WHERE user_id=? AND rule_id=? AND status='expected' AND expected_on>=?", (user_id, rule_id, effective_value))
        return True


def set_income_rule_status(user_id: int, rule_id: int, status: str) -> bool:
    if status not in ("active", "paused", "cancelled"):
        raise ValueError("invalid income rule status")
    with connect() as db:
        row = db.execute("SELECT status FROM income_rules WHERE id=? AND user_id=?", (rule_id, user_id)).fetchone()
        if not row:
            return False
        if row["status"] == "cancelled":
            return status == "cancelled"
        db.execute("UPDATE income_rules SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (status, rule_id, user_id))
        if status == "cancelled":
            db.execute("UPDATE income_records SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND rule_id=? AND status='expected'", (user_id, rule_id))
        return True


def set_income_status(user_id: int, income_id: int, status: str, received_value=None) -> bool:
    if status not in ("received", "cancelled"):
        raise ValueError("invalid income status")
    received_on = None
    if status == "received":
        received_on = date.fromisoformat(received_value or date.today().isoformat())
        if received_on.isoformat() != (received_value or date.today().isoformat()) or received_on > date.today():
            raise ValueError("invalid receipt date")
    with connect() as db:
        row = db.execute("SELECT status FROM income_records WHERE id=? AND user_id=?", (income_id, user_id)).fetchone()
        if not row or row["status"] == "cancelled" or row["status"] == "received" and status != "received":
            return False
        db.execute("UPDATE income_records SET status=?,received_on=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?", (status, received_on.isoformat() if received_on else None, income_id, user_id))
        return True


def delete_income(user_id: int, income_id: int) -> bool:
    with connect() as db:
        row = db.execute("SELECT rule_id,status FROM income_records WHERE id=? AND user_id=?", (income_id, user_id)).fetchone()
        if not row or row["rule_id"] is not None or row["status"] == "received":
            return False
        cur = db.execute("DELETE FROM income_records WHERE id=? AND user_id=? AND status!='received'", (income_id, user_id))
        return bool(cur.rowcount)


def income_totals(user_id: int, month: str, as_of: date | None = None):
    start, end = month_bounds(month)
    as_of = as_of or date.today()
    with connect() as db:
        rows = db.execute("SELECT r.amount_minor,r.status,r.expected_on,r.received_on FROM income_records r LEFT JOIN income_rules q ON q.id=r.rule_id AND q.user_id=r.user_id WHERE r.user_id=? AND r.status!='cancelled' AND (r.rule_id IS NULL OR q.status='active') AND ((r.status='received' AND r.received_on>=? AND r.received_on<?) OR (r.status='expected' AND r.expected_on>=? AND r.expected_on<?))", (user_id,start,end,start,end)).fetchall()
    received = sum(int(row["amount_minor"]) for row in rows if row["status"] == "received")
    expected = sum(int(row["amount_minor"]) for row in rows if row["status"] == "expected" and row["expected_on"] >= as_of.isoformat())
    return {"received_minor": received, "expected_minor": expected}


def income_overview(user_id: int, month: str, as_of: date | None = None):
    as_of = as_of or date.today()
    month_start, month_end = month_bounds(month)
    window_start = date.fromisoformat(month_start)
    window_end = date.fromisoformat(month_end)
    if month >= as_of.strftime("%Y-%m"):
        window_end = max(window_end, as_of + timedelta(days=46))
    with LOCK, connect() as db:
        rules = db.execute("SELECT q.*, (SELECT amount_minor FROM income_rule_amounts a WHERE a.rule_id=q.id AND a.user_id=q.user_id AND a.effective_on<=? ORDER BY a.effective_on DESC LIMIT 1) amount_minor, (SELECT amount_minor FROM income_rule_amounts a WHERE a.rule_id=q.id AND a.user_id=q.user_id AND a.effective_on>? ORDER BY a.effective_on LIMIT 1) next_amount_minor, (SELECT effective_on FROM income_rule_amounts a WHERE a.rule_id=q.id AND a.user_id=q.user_id AND a.effective_on>? ORDER BY a.effective_on LIMIT 1) next_effective_on FROM income_rules q WHERE user_id=? ORDER BY status,description", (as_of.isoformat(), as_of.isoformat(), as_of.isoformat(), user_id)).fetchall()
        for rule in rules:
            if rule["status"] != "active":
                continue
            start_on = date.fromisoformat(rule["start_on"])
            end_on = date.fromisoformat(rule["end_on"]) if rule["end_on"] else None
            from_date = max(start_on, window_start)
            index = income_recurrence_index(start_on, rule["frequency"], from_date, rule["schedule_mode"])
            while True:
                try:
                    occurrence = income_recurrence_date(start_on, rule["frequency"], index, rule["schedule_mode"])
                except (ValueError, OverflowError):
                    break
                if occurrence >= window_end or end_on and occurrence > end_on:
                    break
                if occurrence >= window_start:
                    amount_row = db.execute("SELECT amount_minor FROM income_rule_amounts WHERE user_id=? AND rule_id=? AND effective_on<=? ORDER BY effective_on DESC LIMIT 1", (user_id, rule["id"], occurrence.isoformat())).fetchone()
                    if amount_row:
                        db.execute("INSERT OR IGNORE INTO income_records(user_id,rule_id,category_id,income_type,description,amount_minor,expected_on) VALUES(?,?,?,?,?,?,?)",
                                   (user_id, rule["id"], rule["category_id"], rule["income_type"], rule["description"], amount_row["amount_minor"], occurrence.isoformat()))
                index += 1
        rows = db.execute("SELECT r.*,u.status status_rule,c.name category FROM income_records r LEFT JOIN income_rules u ON u.id=r.rule_id AND u.user_id=r.user_id LEFT JOIN categories c ON c.id=r.category_id AND c.user_id=r.user_id WHERE r.user_id=? AND r.expected_on>=? AND r.expected_on<? ORDER BY r.expected_on,r.id", (user_id,month_start,window_end.isoformat())).fetchall()
        rules = db.execute("SELECT q.*, (SELECT amount_minor FROM income_rule_amounts a WHERE a.rule_id=q.id AND a.user_id=q.user_id AND a.effective_on<=? ORDER BY a.effective_on DESC LIMIT 1) amount_minor, (SELECT amount_minor FROM income_rule_amounts a WHERE a.rule_id=q.id AND a.user_id=q.user_id AND a.effective_on>? ORDER BY a.effective_on LIMIT 1) next_amount_minor, (SELECT effective_on FROM income_rule_amounts a WHERE a.rule_id=q.id AND a.user_id=q.user_id AND a.effective_on>? ORDER BY a.effective_on LIMIT 1) next_effective_on FROM income_rules q WHERE user_id=? ORDER BY status,description", (as_of.isoformat(), as_of.isoformat(), as_of.isoformat(), user_id)).fetchall()
    records=[]
    for raw in rows:
        item=dict(raw)
        item.pop("status_rule",None)
        item["category"] = item.get("category") or ""
        if item["rule_id"] and item.get("status") == "expected":
            rule_status = raw["status_rule"]
            if rule_status != "active":
                item["status"] = rule_status
            elif item["expected_on"] < as_of.isoformat():
                item["status"] = "overdue"
        records.append(item)
    totals=income_totals(user_id,month,as_of)
    return {"records":records,"rules":[dict(r) for r in rules],**totals}


def save_opening_cash(user_id: int, amount: str, effective_value: str):
    """Set the confirmed cash-and-bank balance that anchors subsequent cash accounting."""
    amount_minor = money_to_minor(amount)
    effective_on = date.fromisoformat(effective_value)
    if amount_minor < 0 or amount_minor > MAX_MINOR or effective_on.isoformat() != effective_value or effective_on > date.today():
        raise ValueError("invalid opening cash")
    with connect() as db:
        db.execute("INSERT INTO cash_settings(user_id,opening_balance_minor,effective_on) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET opening_balance_minor=excluded.opening_balance_minor,effective_on=excluded.effective_on,updated_at=CURRENT_TIMESTAMP",
                   (user_id, amount_minor, effective_value))
    return get_cash_position(user_id)


def add_cash_adjustment(user_id: int, amount: str, adjustment_value: str, description: str):
    if not isinstance(amount, (str, int)) or isinstance(amount, bool) or not isinstance(description, str):
        raise ValueError("invalid cash adjustment")
    raw = str(amount).strip()
    sign = -1 if raw.startswith("-") else 1
    if raw[:1] in ("-", "+"):
        raw = raw[1:]
    amount_minor = sign * money_to_minor(raw)
    adjustment_on = date.fromisoformat(adjustment_value)
    description = description.strip()
    if not amount_minor or abs(amount_minor) > MAX_MINOR or adjustment_on.isoformat() != adjustment_value or adjustment_on > date.today() or not description or len(description) > 160:
        raise ValueError("invalid cash adjustment")
    with connect() as db:
        settings = db.execute("SELECT effective_on FROM cash_settings WHERE user_id=?", (user_id,)).fetchone()
        if not settings or adjustment_value < settings["effective_on"]:
            raise ValueError("adjustment must be on or after the opening date")
        cursor = db.execute("INSERT INTO cash_adjustments(user_id,adjustment_on,amount_minor,description) VALUES(?,?,?,?)",
                            (user_id, adjustment_value, amount_minor, description))
        return cursor.lastrowid


def get_cash_position(user_id: int, as_of: date | None = None):
    """Return confirmed balance only when an explicit opening balance anchors history."""
    as_of = as_of or date.today()
    with connect() as db:
        settings = db.execute("SELECT opening_balance_minor,effective_on FROM cash_settings WHERE user_id=?", (user_id,)).fetchone()
        adjustments = [dict(row) for row in db.execute("SELECT id,adjustment_on,amount_minor,description FROM cash_adjustments WHERE user_id=? ORDER BY adjustment_on,id", (user_id,)).fetchall()]
        if not settings:
            return {"configured": False, "reliable": False, "balance_minor": None, "opening_balance_minor": None, "effective_on": None, "adjustments": adjustments}
        effective = settings["effective_on"]
        received = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM income_records WHERE user_id=? AND status='received' AND received_on>=? AND received_on<=?", (user_id,effective,as_of.isoformat())).fetchone()[0]
        cash_expenses = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM expenses WHERE user_id=? AND card_id IS NULL AND spent_on>=? AND spent_on<=?", (user_id,effective,as_of.isoformat())).fetchone()[0]
        repayments = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM card_repayments WHERE user_id=? AND paid_on>=? AND paid_on<=?", (user_id,effective,as_of.isoformat())).fetchone()[0]
        adjustments_minor = sum(int(row["amount_minor"]) for row in adjustments if effective <= row["adjustment_on"] <= as_of.isoformat())
        balance = int(settings["opening_balance_minor"]) + int(received) - int(cash_expenses) - int(repayments) + adjustments_minor
    return {"configured": True, "reliable": True, "balance_minor": balance, "opening_balance_minor": int(settings["opening_balance_minor"]), "effective_on": effective, "adjustments": adjustments}


def _month_range(start: date, end: date):
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor.strftime("%Y-%m")
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)


def payment_calendar(user_id: int, start_value: str, end_value: str, as_of: date | None = None):
    """Build user-scoped calendar events from the existing authoritative records."""
    as_of = as_of or date.today()
    start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
    if start.isoformat() != start_value or end.isoformat() != end_value or end < start or (end - start).days > 366:
        raise ValueError("invalid calendar range")
    events = []
    start_text, end_exclusive = start.isoformat(), (end + timedelta(days=1)).isoformat()
    def add(event_date, amount, description, status, event_type, source_type, source_id, **extra):
        events.append({"date":event_date,"amount_minor":int(amount),"description":description,"status":status,"event_type":event_type,
                       "source_type":source_type,"source_id":source_id,**extra})

    # Existing expenses are spending events; only non-card expenses change confirmed cash.
    with connect() as db:
        expenses = db.execute("SELECT e.id,e.amount_minor,e.spent_on,e.description,e.card_id,e.payment_method_id,p.name payment_method FROM expenses e LEFT JOIN payment_methods p ON p.id=e.payment_method_id AND p.user_id=e.user_id WHERE e.user_id=? AND e.spent_on>=? AND e.spent_on<? ORDER BY e.spent_on,e.id", (user_id,start_text,end_exclusive)).fetchall()
        income_rows = db.execute("SELECT id,amount_minor,expected_on,received_on,status,description,income_type FROM income_records WHERE user_id=? AND expected_on<? AND (expected_on>=? OR (received_on>=? AND status='received')) ORDER BY expected_on,id", (user_id,end_exclusive,start_text,start_text)).fetchall()
        adjustments = db.execute("SELECT id,amount_minor,adjustment_on,description FROM cash_adjustments WHERE user_id=? AND adjustment_on>=? AND adjustment_on<? ORDER BY adjustment_on,id", (user_id,start_text,end_exclusive)).fetchall()
        repayment_rows = db.execute("SELECT id,card_id,statement_id,amount_minor,paid_on,note FROM card_repayments WHERE user_id=? AND paid_on>=? AND paid_on<? ORDER BY paid_on,id", (user_id,start_text,end_exclusive)).fetchall()
        rule_rows = db.execute("SELECT r.*,p.name payment_method FROM recurring_expenses r LEFT JOIN payment_methods p ON p.id=r.payment_method_id AND p.user_id=r.user_id WHERE r.user_id=? AND r.status IN ('active','paused')", (user_id,)).fetchall()
        occurrence_rows = db.execute("SELECT recurring_id,scheduled_on FROM recurring_occurrences WHERE user_id=?", (user_id,)).fetchall()
    for row in expenses:
        add(row["spent_on"],row["amount_minor"],row["description"],"completed","expense","expense",row["id"],cash_effect_minor=0 if row["card_id"] else -int(row["amount_minor"]),payment_method=row["payment_method"],card_id=row["card_id"])

    # Materialize any existing recurrence records through the requested window; UNIQUE(rule,date) keeps this idempotent.
    for month in _month_range(start,end):
        income_overview(user_id,month,as_of)
    with connect() as db:
        income_rows = db.execute("SELECT r.id,r.amount_minor,r.expected_on,r.received_on,r.status,r.rule_id,r.description,r.income_type,q.status rule_status FROM income_records r LEFT JOIN income_rules q ON q.id=r.rule_id AND q.user_id=r.user_id WHERE r.user_id=? AND ((r.status='received' AND r.received_on>=? AND r.received_on<?) OR (r.status!='received' AND r.expected_on>=? AND r.expected_on<?)) ORDER BY COALESCE(r.received_on,r.expected_on),r.id", (user_id,start_text,end_exclusive,start_text,end_exclusive)).fetchall()
    for row in income_rows:
        event_date = row["received_on"] if row["status"] == "received" else row["expected_on"]
        status = row["status"]
        if row["status"]=="expected" and row["rule_id"] and row["rule_status"]!="active": status=row["rule_status"] or "cancelled"
        if status == "expected" and event_date < as_of.isoformat(): status = "overdue"
        add(event_date,row["amount_minor"],row["description"],status,"income","income",row["id"],cash_effect_minor=int(row["amount_minor"]) if status in ("received","expected") else 0,income_type=row["income_type"],expected_on=row["expected_on"],received_on=row["received_on"])

    completed_keys = {(row["recurring_id"],row["scheduled_on"]) for row in occurrence_rows}
    for rule in rule_rows:
        start_on = date.fromisoformat(rule["start_on"])
        end_on = date.fromisoformat(rule["end_on"]) if rule["end_on"] else None
        index = recurrence_index_on_or_after(start_on,rule["frequency"],start)
        while True:
            try: occurrence = recurrence_date(start_on,rule["frequency"],index)
            except (ValueError,OverflowError): break
            if occurrence > end or end_on and occurrence > end_on: break
            key=(rule["id"],occurrence.isoformat())
            if occurrence >= start and key not in completed_keys:
                is_card = "kredi kart" in str(rule["payment_method"] or "").casefold()
                unknown_method=not rule["payment_method"]
                included=bool(not is_card and not unknown_method and rule["status"]=="active")
                add(occurrence.isoformat(),rule["amount_minor"],rule["description"],"planned" if rule["status"]=="active" else "paused","recurring_expense","recurring_rule",rule["id"],cash_effect_minor=-int(rule["amount_minor"]) if included else 0,payment_method=rule["payment_method"],forecast_included=included,forecast_issue="payment_method_missing" if unknown_method else "card_not_assigned" if is_card else None,scheduled_on=occurrence.isoformat())
            index += 1

    with connect() as db:
        cards = db.execute("SELECT id,display_name,opening_balance_minor FROM credit_cards WHERE user_id=?",(user_id,)).fetchall()
        installments = db.execute("SELECT o.id,o.user_id,o.ordinal,o.due_on,o.paid_on,o.amount_minor,o.status,o.statement_id,p.card_id,e.description,cc.display_name FROM installment_occurrences o JOIN installment_plans p ON p.id=o.plan_id AND p.user_id=o.user_id JOIN expenses e ON e.id=p.expense_id AND e.user_id=p.user_id JOIN credit_cards cc ON cc.id=p.card_id AND cc.user_id=p.user_id WHERE o.user_id=? AND (o.due_on>=? OR (o.status='paid' AND o.paid_on>=?)) AND (o.due_on<? OR o.paid_on<?) ORDER BY COALESCE(o.paid_on,o.due_on),o.id",(user_id,start_text,start_text,end_exclusive,end_exclusive)).fetchall()
    installment_ids = {int(row["id"]) for row in installments}
    for row in repayment_rows:
        if row["note"] == "Taksit ödemesi":
            continue
        add(row["paid_on"],row["amount_minor"],"Kredi kartı ödemesi", "completed","card_repayment","card_repayment",row["id"],cash_effect_minor=-int(row["amount_minor"]),card_id=row["card_id"],statement_id=row["statement_id"])
    for row in installments:
        if row["status"] == "cancelled": continue
        event_date=row["paid_on"] if row["status"]=="paid" else row["due_on"]
        add(event_date,row["amount_minor"],f"{row['description']} · {row['ordinal']}. taksit",row["status"],"installment","installment_occurrence",row["id"],cash_effect_minor=-int(row["amount_minor"]) if row["status"]=="paid" else 0,card_name=row["display_name"],statement_id=row["statement_id"],forecast_included=False,explanation="Ekstre tutarına dahildir; tahmin ekstre ödeme tarihini kullanır.")
    for row in adjustments:
        add(row["adjustment_on"],abs(row["amount_minor"]),row["description"],"completed","cash_adjustment","cash_adjustment",row["id"],cash_effect_minor=int(row["amount_minor"]),signed_amount_minor=int(row["amount_minor"]))
    for card in cards:
        statements = list_card_statements(user_id,card["id"]) or []
        for statement in statements:
            if not (start_text <= statement["due_on"] < end_exclusive): continue
            status = "paid" if statement["remaining_minor"] == 0 and statement["total_minor"] else "overdue" if statement["due_on"] < as_of.isoformat() and statement["remaining_minor"] else "partial" if statement["paid_minor"] else "expected"
            add(statement["due_on"],statement["remaining_minor"],f"{card['display_name']} · Ekstre ödemesi",status,"card_statement","card_statement",statement["id"],cash_effect_minor=-int(statement["remaining_minor"]) if statement["due_on"]>=as_of.isoformat() else 0,card_id=card["id"],closing_on=statement["closing_on"],due_on=statement["due_on"],statement_total_minor=statement["total_minor"],paid_minor=statement["paid_minor"],remaining_minor=statement["remaining_minor"],forecast_included=bool(statement["remaining_minor"] and statement["due_on"]>=as_of.isoformat()))
    events.sort(key=lambda event:(event["date"],0 if event["event_type"]=="income" else 1,event["event_type"],str(event["source_id"])))
    return events


def cash_flow_forecast(user_id: int, start_value: str | None = None, end_value: str | None = None, as_of: date | None = None):
    as_of = as_of or date.today()
    start_value = start_value or as_of.isoformat()
    if end_value is None:
        month_index = as_of.year*12+as_of.month-1+2
        year,month0=divmod(month_index,12)
        end_value=date(year,month0+1,calendar.monthrange(year,month0+1)[1]).isoformat()
    requested_start=date.fromisoformat(start_value)
    if requested_start.isoformat()!=start_value or date.fromisoformat(end_value).isoformat()!=end_value or date.fromisoformat(end_value)<as_of:
        raise ValueError("forecast end must be today or later")
    event_start=min(requested_start,as_of)
    events=payment_calendar(user_id,event_start.isoformat(),end_value,as_of)
    position=get_cash_position(user_id,as_of)
    cards=list_credit_cards(user_id)
    liabilities=sum(int(card["outstanding_minor"]) for card in cards)
    warnings=[]
    for event in events:
        if event["event_type"]=="recurring_expense" and event.get("forecast_issue"):
            reason="Düzenli harcamanın ödeme yöntemi belirtilmediği için nakit çıkışı tahmine eklenmedi." if event["forecast_issue"]=="payment_method_missing" else "Düzenli kart harcaması belirli bir karta/ekstreye bağlı olmadığından ödeme vadesi tahmin edilemiyor."
            warnings.append({"level":"insufficient","date":event["date"],"amount_minor":event["amount_minor"],"reason":f"{event['description']}: {reason}"})
    if not position["reliable"]:
        warnings.append({"level":"insufficient","date":as_of.isoformat(),"amount_minor":None,"reason":"Güvenilir nakit tahmini için başlangıç nakit bakiyesi ve başlangıç tarihi girilmelidir."})
    else:
        balance=int(position["balance_minor"])
        for event in events:
            event_date=event["date"]
            is_future_cash = (event["event_type"]=="income" and event["status"]=="expected" and event_date>=as_of.isoformat()) or (event["event_type"]=="recurring_expense" and event.get("forecast_included") and event_date>=as_of.isoformat()) or (event["event_type"]=="card_statement" and event.get("forecast_included") and event_date>=as_of.isoformat()) or (event["event_type"]=="cash_adjustment" and event_date>as_of.isoformat())
            if event["event_type"]=="expense" and event_date>as_of.isoformat() and event.get("cash_effect_minor"):
                is_future_cash=True
            if not is_future_cash: continue
            before=balance
            balance += int(event.get("cash_effect_minor",0))
            event["projected_balance_minor"]=balance
            if balance < 0:
                warnings.append({"level":"high","date":event_date,"amount_minor":abs(balance),"reason":f"{event['description']} sonrasında tahmini nakit eksiye düşüyor; önceki bakiye bu ödemeyi karşılamıyor."})
            elif event["event_type"]=="card_statement" and int(event["remaining_minor"])>before:
                warnings.append({"level":"high","date":event_date,"amount_minor":int(event["remaining_minor"]),"reason":"Ekstre ödemesi tahmini kullanılabilir nakit bakiyesini aşabilir."})
        end_balance=balance
        if end_balance < 0:
            warnings.append({"level":"high","date":end_value,"amount_minor":abs(end_balance),"reason":"Dönem sonu tahmini nakit bakiyesi eksi."})
        incomes=sum(int(e["amount_minor"]) for e in events if e["event_type"]=="income" and e["status"]=="expected" and e["date"]>=as_of.isoformat())
        obligations=sum(-int(e.get("cash_effect_minor",0)) for e in events if e.get("forecast_included") and e["cash_effect_minor"]<0 and e["date"]>=as_of.isoformat())
        if obligations>incomes and obligations:
            warnings.append({"level":"medium","date":end_value,"amount_minor":obligations-incomes,"reason":"Seçili dönemdeki beklenen gelir, planlanan nakit çıkışlarının tamamını karşılamıyor."})
        if liabilities > max(0,int(position["balance_minor"])):
            warnings.append({"level":"medium","date":as_of.isoformat(),"amount_minor":liabilities,"reason":"Toplam kredi kartı borcu, doğrulanmış nakit bakiyesinden yüksek."})
    if any(card["opening_balance_minor"] for card in cards):
        warnings.append({"level":"insufficient","date":as_of.isoformat(),"amount_minor":sum(int(card["opening_balance_minor"]) for card in cards if card["opening_balance_minor"]),"reason":"Kart açılış borçları için ekstre vadesi tanımlı değil; bu tutarlar ödeme takvimine dağıtılamıyor."})
    for event in events:
        if event["event_type"]=="card_statement" and event["status"]=="overdue":
            warnings.append({"level":"high","date":event["date"],"amount_minor":event["amount_minor"],"reason":f"{event['description']} gecikmiş ve ödenmemiş."})
    warned_statements={event["source_id"] for event in events if event["event_type"]=="card_statement" and event["status"]=="overdue"}
    for card in cards:
        for statement in list_card_statements(user_id,card["id"]) or []:
            if statement["remaining_minor"] and statement["due_on"]<as_of.isoformat() and statement["id"] not in warned_statements:
                warnings.append({"level":"high","date":statement["due_on"],"amount_minor":statement["remaining_minor"],"reason":f"{card['display_name']} · Ekstre ödemesi gecikmiş ve ödenmemiş."})
    included = [event for event in events if event.get("projected_balance_minor") is not None]
    risk="insufficient" if not position["reliable"] or any(w["level"]=="insufficient" for w in warnings) else "high" if any(w["level"]=="high" for w in warnings) else "medium" if warnings else "low"
    return {"as_of":as_of.isoformat(),"start_on":as_of.isoformat(),"end_on":end_value,"configured":position["configured"],"starting_cash_minor":position["balance_minor"],"projected_end_minor":end_balance if position["reliable"] else None,"outstanding_card_liability_minor":liabilities,"remaining_installments_minor":sum(int(row["amount_minor"]) for row in list_installments(user_id) if row["status"]=="unpaid"),"events":events,"projection_events":included,"warnings":warnings,"risk":risk}


def export_income_csv(user_id: int) -> str:
    """Export only the caller's income records; recurring rules stay in the app."""
    with connect() as db:
        rows=db.execute("SELECT income_type,description,amount_minor,expected_on,received_on,status,note FROM income_records WHERE user_id=? ORDER BY expected_on,id",(user_id,)).fetchall()
    out=io.StringIO(newline="")
    writer=csv.writer(out,delimiter=";",lineterminator="\r\n")
    writer.writerow(["Tür","Açıklama","Tutar","Beklenen tarih","Alınma tarihi","Durum","Not"])
    def safe(value):
        value=str(value or "")
        return "'"+value if value.lstrip().startswith(("=","+","-","@")) else value
    for row in rows:
        minor=int(row["amount_minor"])
        amount=f"{minor//100},{minor%100:02d}"
        writer.writerow([safe(row["income_type"]),safe(row["description"]),amount,row["expected_on"],row["received_on"] or "",row["status"],safe(row["note"])])
    return "\ufeff"+out.getvalue()


def user_options(user_id: int):
    with connect() as db:
        categories = db.execute("SELECT id,name FROM categories WHERE user_id=? AND archived=0 ORDER BY name", (user_id,)).fetchall()
        methods = db.execute("SELECT id,name FROM payment_methods WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
    return {"categories":[dict(r) for r in categories],"payment_methods":[dict(r) for r in methods],"credit_cards":list_credit_cards(user_id)}


def money_to_minor(value):
    if isinstance(value, bool): raise ValueError("invalid amount")
    text = str(value).strip().replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."): text = text.replace(".", "").replace(",", ".")
        else: text = text.replace(",", "")
    elif "," in text: text = text.replace(",", ".")
    whole, dot, fraction = text.partition(".")
    if not whole.isdigit() or (fraction and (not fraction.isdigit() or len(fraction) > 2)):
        raise ValueError("invalid amount")
    minor = int(whole)*100 + int((fraction+"00")[:2])
    if minor > MAX_MINOR:
        raise ValueError("amount too large")
    return minor


def record_id(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not str(value).isdigit():
        raise ValueError("invalid record id")
    result = int(value)
    if not 1 <= result <= 9_000_000_000_000_000_000:
        raise ValueError("invalid record id")
    return result


def _month_close(year: int, month: int, day_number: int) -> date:
    return date(year, month, min(day_number, calendar.monthrange(year, month)[1]))


def next_card_closing_date(closing_day: int, as_of: date | None = None) -> date:
    as_of = as_of or date.today()
    closing = _month_close(as_of.year, as_of.month, closing_day)
    if closing < as_of:
        year, month = as_of.year + (as_of.month == 12), (as_of.month % 12) + 1
        closing = _month_close(year, month, closing_day)
    return closing


def statement_due_date(closing_day: int, due_day: int, closing_on: date) -> date:
    """Use the configured day pair to choose the due-date month, clamping short months."""
    due = _month_close(closing_on.year, closing_on.month, due_day)
    if due_day <= closing_day or due <= closing_on:
        due_year, due_month = closing_on.year + (closing_on.month == 12), (closing_on.month % 12) + 1
        due = _month_close(due_year, due_month, due_day)
    return due


def card_closing_on_or_after(period_start: date, closing_day: int) -> date:
    closing = _month_close(period_start.year, period_start.month, closing_day)
    if closing < period_start:
        year, month = period_start.year + (period_start.month == 12), (period_start.month % 12) + 1
        closing = _month_close(year, month, closing_day)
    return closing


def _legacy_interval_for_due_day(closing_day: int, due_day: int) -> int:
    closing = next_card_closing_date(closing_day)
    due = statement_due_date(closing_day, due_day, closing)
    return min(90, max(1, (due - closing).days))


def _legacy_due_day(closing_day: int, due_days_after_close: int) -> int:
    closing = next_card_closing_date(closing_day)
    return (closing + timedelta(days=due_days_after_close)).day


def card_cycle(closing_day: int, due_day: int, spent_on: date):
    """Purchases on the closing date belong to the statement closing that day."""
    close = _month_close(spent_on.year, spent_on.month, closing_day)
    if spent_on > close:
        year, month = spent_on.year + (spent_on.month == 12), (spent_on.month % 12) + 1
        close = _month_close(year, month, closing_day)
    previous_month = date(close.year, close.month, 1) - timedelta(days=1)
    previous_close = _month_close(previous_month.year, previous_month.month, closing_day)
    return previous_close + timedelta(days=1), close, statement_due_date(closing_day, due_day, close)


def _get_or_create_statement(db, user_id: int, card_id: int, spent_on: date, allow_inactive=False):
    card = db.execute("SELECT * FROM credit_cards WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
    if not card or (not card["active"] and not allow_inactive):
        raise ValueError("invalid card")
    period_start, closing_on, due_on = card_cycle(card["closing_day"], card["due_day"], spent_on)
    db.execute("INSERT OR IGNORE INTO card_statements(user_id,card_id,period_start,closing_on,due_on) VALUES(?,?,?,?,?)",
               (user_id, card_id, period_start.isoformat(), closing_on.isoformat(), due_on.isoformat()))
    return db.execute("SELECT id FROM card_statements WHERE card_id=? AND period_start=?", (card_id, period_start.isoformat())).fetchone()["id"]


def save_credit_card(user_id: int, data: dict) -> int:
    bank = str(data.get("bank_name", "")).strip()
    display = str(data.get("display_name", "")).strip()
    nickname = str(data.get("nickname", "")).strip()
    last_four = str(data.get("last_four", "")).strip()
    if not bank or not display or len(bank) > 80 or len(display) > 80 or len(nickname) > 80 or (last_four and (len(last_four) != 4 or not last_four.isdigit())):
        raise ValueError("invalid card details")
    try:
        closing_day = int(data.get("closing_day"))
        if data.get("due_day") not in (None, ""):
            due_day = int(data.get("due_day"))
            due_days = _legacy_interval_for_due_day(closing_day, due_day)
        else:
            # Retain compatibility for callers using the previous API contract.
            due_days = int(data.get("due_days_after_close"))
            due_day = _legacy_due_day(closing_day, due_days)
    except (TypeError, ValueError):
        raise ValueError("invalid card schedule") from None
    if not 1 <= closing_day <= 31 or not 1 <= due_day <= 31 or not 1 <= due_days <= 90:
        raise ValueError("invalid card schedule")
    limit = money_to_minor(data["credit_limit"]) if data.get("credit_limit") not in (None, "") else None
    opening = money_to_minor(data.get("opening_balance", "0"))
    if limit is not None and not 0 < limit <= MAX_MINOR or not 0 <= opening <= MAX_MINOR:
        raise ValueError("invalid card balance")
    card_id = record_id(data.get("id"))
    active_value = data.get("active")
    active = None if active_value is None else (0 if active_value is False or active_value == 0 else 1)
    with connect() as db:
        if card_id:
            existing = db.execute("SELECT id,active FROM credit_cards WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
            if not existing:
                return 0
            if active is None: active = existing["active"]
            db.execute("UPDATE credit_cards SET bank_name=?,display_name=?,nickname=?,last_four=?,closing_day=?,due_day=?,due_days_after_close=?,credit_limit_minor=?,opening_balance_minor=?,active=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                       (bank, display, nickname, last_four, closing_day, due_day, due_days, limit, opening, active, card_id, user_id))
            _recalculate_future_statement_dates(db, user_id, card_id, closing_day, due_day)
            return card_id
        cur = db.execute("INSERT INTO credit_cards(user_id,bank_name,display_name,nickname,last_four,closing_day,due_day,due_days_after_close,credit_limit_minor,opening_balance_minor,active) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (user_id, bank, display, nickname, last_four, closing_day, due_day, due_days, limit, opening, active if active is not None else 1))
        return cur.lastrowid


def _recalculate_future_statement_dates(db, user_id: int, card_id: int, closing_day: int, due_day: int):
    today = date.today().isoformat()
    statements = db.execute("SELECT id,period_start FROM card_statements WHERE user_id=? AND card_id=? AND closing_on>=? AND dates_manually_corrected=0 AND NOT EXISTS (SELECT 1 FROM card_repayments r WHERE r.user_id=card_statements.user_id AND r.card_id=card_statements.card_id AND r.statement_id=card_statements.id)", (user_id, card_id, today)).fetchall()
    for statement in statements:
        period_start = date.fromisoformat(statement["period_start"])
        closing = card_closing_on_or_after(period_start, closing_day)
        due = statement_due_date(closing_day, due_day, closing)
        db.execute("UPDATE card_statements SET closing_on=?,due_on=? WHERE id=? AND user_id=? AND card_id=?", (closing.isoformat(), due.isoformat(), statement["id"], user_id, card_id))


def list_credit_cards(user_id: int):
    with connect() as db:
        cards = db.execute("SELECT * FROM credit_cards WHERE user_id=? ORDER BY active DESC,display_name", (user_id,)).fetchall()
        result = []
        for raw in cards:
            card = dict(raw)
            card["outstanding_minor"] = max(0, int(card["opening_balance_minor"]) + int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM expenses WHERE user_id=? AND card_id=?", (user_id, card["id"])).fetchone()[0]) - int(db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM card_repayments WHERE user_id=? AND card_id=?", (user_id, card["id"])).fetchone()[0]))
            result.append(card)
        return result


def list_card_statements(user_id: int, card_id: int):
    with connect() as db:
        card = db.execute("SELECT id FROM credit_cards WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()
        if not card:
            return None
        statements = db.execute("SELECT * FROM card_statements WHERE user_id=? AND card_id=? ORDER BY closing_on DESC", (user_id, card_id)).fetchall()
        output = []
        for raw in statements:
            item = dict(raw)
            purchase = db.execute("SELECT COALESCE(SUM(e.amount_minor),0) FROM expenses e WHERE e.user_id=? AND e.card_id=? AND e.statement_id=? AND NOT EXISTS(SELECT 1 FROM installment_plans p WHERE p.expense_id=e.id)", (user_id, card_id, item["id"])).fetchone()[0]
            installments = db.execute("SELECT COALESCE(SUM(o.amount_minor),0) FROM installment_occurrences o JOIN installment_plans p ON p.id=o.plan_id WHERE o.user_id=? AND p.card_id=? AND o.statement_id=? AND o.status!='cancelled'", (user_id, card_id, item["id"])).fetchone()[0]
            paid = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM card_repayments WHERE user_id=? AND statement_id=?", (user_id, item["id"])).fetchone()[0]
            item["total_minor"] = int(purchase) + int(installments)
            item["paid_minor"] = int(paid)
            item["remaining_minor"] = max(0, item["total_minor"] - item["paid_minor"])
            item["status"] = "paid" if item["remaining_minor"] == 0 and item["total_minor"] else "overdue" if item["due_on"] < date.today().isoformat() else "partial" if item["paid_minor"] else "unpaid"
            output.append(item)
        return output


def edit_statement_dates(user_id: int, statement_id: int, closing_on_value: str, due_on_value: str):
    closing = date.fromisoformat(closing_on_value); due = date.fromisoformat(due_on_value)
    if closing.isoformat() != closing_on_value or due.isoformat() != due_on_value or due < closing:
        raise ValueError("invalid statement dates")
    with connect() as db:
        existing = db.execute("SELECT period_start FROM card_statements WHERE id=? AND user_id=?", (statement_id,user_id)).fetchone()
        if not existing:
            return False
        if closing_on_value < existing["period_start"]:
            raise ValueError("statement closing date precedes its period")
        cur = db.execute("UPDATE card_statements SET closing_on=?,due_on=?,dates_manually_corrected=1 WHERE id=? AND user_id=?", (closing_on_value,due_on_value,statement_id,user_id))
        return bool(cur.rowcount)


def record_card_repayment(user_id: int, card_id: int, data: dict):
    amount = money_to_minor(data.get("amount"))
    paid_value = data.get("paid_on", date.today().isoformat())
    paid_on = date.fromisoformat(paid_value)
    note = data.get("note", "")
    if amount <= 0 or amount > MAX_MINOR or paid_on.isoformat() != paid_value or paid_on > date.today() or not isinstance(note, str) or len(note) > 500:
        raise ValueError("invalid repayment")
    statement_id = record_id(data.get("statement_id"))
    with connect() as db:
        if not db.execute("SELECT 1 FROM credit_cards WHERE id=? AND user_id=?", (card_id,user_id)).fetchone():
            return 0
        if statement_id and not db.execute("SELECT 1 FROM card_statements WHERE id=? AND card_id=? AND user_id=?", (statement_id,card_id,user_id)).fetchone():
            return 0
        outstanding = int(db.execute("SELECT opening_balance_minor+COALESCE((SELECT SUM(amount_minor) FROM expenses WHERE user_id=? AND card_id=?),0)-COALESCE((SELECT SUM(amount_minor) FROM card_repayments WHERE user_id=? AND card_id=?),0) FROM credit_cards WHERE id=? AND user_id=?", (user_id,card_id,user_id,card_id,card_id,user_id)).fetchone()[0])
        if amount > outstanding:
            raise ValueError("repayment exceeds outstanding balance")
        cur = db.execute("INSERT INTO card_repayments(user_id,card_id,statement_id,amount_minor,paid_on,note) VALUES(?,?,?,?,?,?)", (user_id,card_id,statement_id,amount,paid_value,note.strip()))
        return cur.lastrowid


def _create_installment_plan_in_db(db, user_id: int, expense_id: int, count: int, first_on_value: str):
    first_on = date.fromisoformat(first_on_value)
    if first_on.isoformat() != first_on_value or count < 2 or count > 120:
        raise ValueError("invalid installment schedule")
    expense = db.execute("SELECT amount_minor,card_id,spent_on FROM expenses WHERE id=? AND user_id=?", (expense_id,user_id)).fetchone()
    if not expense or not expense["card_id"]:
        raise ValueError("installments require an owned card purchase")
    if first_on < date.fromisoformat(expense["spent_on"]):
        raise ValueError("first installment cannot precede the purchase")
    if db.execute("SELECT 1 FROM installment_plans WHERE expense_id=?", (expense_id,)).fetchone():
        return 0
    cur = db.execute("INSERT INTO installment_plans(user_id,expense_id,card_id,original_minor,count,first_on) VALUES(?,?,?,?,?,?)", (user_id,expense_id,expense["card_id"],expense["amount_minor"],count,first_on_value))
    plan_id = cur.lastrowid
    base, remainder = divmod(int(expense["amount_minor"]), count)
    for ordinal in range(count):
        month0 = first_on.year * 12 + first_on.month - 1 + ordinal
        due = _month_close(month0 // 12, month0 % 12 + 1, first_on.day)
        statement_id = _get_or_create_statement(db,user_id,expense["card_id"],due)
        db.execute("INSERT INTO installment_occurrences(user_id,plan_id,ordinal,due_on,amount_minor,statement_id) VALUES(?,?,?,?,?,?)", (user_id,plan_id,ordinal+1,due.isoformat(),base+(1 if ordinal<remainder else 0),statement_id))
    db.execute("UPDATE expenses SET installments_count=? WHERE id=? AND user_id=?", (count,expense_id,user_id))
    return plan_id


def create_installment_plan(user_id: int, expense_id: int, count: int, first_on_value: str):
    with connect() as db:
        return _create_installment_plan_in_db(db,user_id,expense_id,count,first_on_value)


def list_installments(user_id: int, card_id: int | None = None):
    with connect() as db:
        sql = "SELECT o.*,p.expense_id,p.card_id,p.count,e.description FROM installment_occurrences o JOIN installment_plans p ON p.id=o.plan_id JOIN expenses e ON e.id=p.expense_id WHERE o.user_id=?"
        args = [user_id]
        if card_id:
            sql += " AND p.card_id=?"; args.append(card_id)
        return [dict(row) for row in db.execute(sql+" ORDER BY o.due_on,o.ordinal",args).fetchall()]


def set_installment_status(user_id: int, occurrence_id: int, status: str, paid_on_value: str | None = None):
    if status not in ("paid", "cancelled", "unpaid"):
        raise ValueError("invalid installment status")
    paid_on = date.fromisoformat(paid_on_value) if paid_on_value else date.today()
    if paid_on.isoformat() != (paid_on_value or paid_on.isoformat()) or paid_on > date.today():
        raise ValueError("invalid installment payment date")
    with connect() as db:
        row = db.execute("SELECT o.status,o.amount_minor,o.statement_id,p.card_id FROM installment_occurrences o JOIN installment_plans p ON p.id=o.plan_id WHERE o.id=? AND o.user_id=?", (occurrence_id,user_id)).fetchone()
        if not row:
            return False
        if row["status"] == "paid" and status == "paid":
            return True
        if row["status"] == "paid" and status != "paid":
            raise ValueError("paid installments are historical records")
        db.execute("UPDATE installment_occurrences SET status=?,paid_on=? WHERE id=? AND user_id=?", (status, paid_on.isoformat() if status=="paid" else None, occurrence_id,user_id))
        if status == "paid":
            db.execute("INSERT INTO card_repayments(user_id,card_id,statement_id,amount_minor,paid_on,note) VALUES(?,?,?,?,?,?)", (user_id,row["card_id"],row["statement_id"],row["amount_minor"],paid_on.isoformat(),"Taksit ödemesi"))
        return True


def save_expense(user_id: int, data: dict) -> int:
    spent_value=data.get("spent_on",""); description_value=data.get("description",""); note=data.get("note","")
    if not isinstance(spent_value,str) or not isinstance(description_value,str) or not isinstance(note,str): raise ValueError("invalid expense fields")
    amount=money_to_minor(data.get("amount")); spent_on=date.fromisoformat(spent_value); description=description_value.strip()
    if spent_on.isoformat() != spent_value or spent_on > date.today() or amount<=0 or not description or len(description)>160 or len(note)>1000: raise ValueError("invalid expense")
    category=record_id(data.get("category_id"))
    method=record_id(data.get("payment_method_id"))
    card_id=record_id(data.get("card_id"))
    try:
        installments_count=int(data.get("installments_count",1) or 1)
    except (TypeError,ValueError):
        raise ValueError("invalid installment count") from None
    first_installment_on=data.get("first_installment_on") or spent_value
    if installments_count < 1 or installments_count > 120 or installments_count > 1 and not card_id:
        raise ValueError("installments require a card and valid count")
    if installments_count > 1:
        first_date=date.fromisoformat(first_installment_on)
        if first_date.isoformat()!=first_installment_on or first_date < spent_on:
            raise ValueError("invalid first installment date")
    currency=str(data.get("currency","TRY")).upper()
    if currency!="TRY": raise ValueError("Only TRY is supported in this MVP")
    with connect() as db:
        expense_id=record_id(data.get("id"))
        card_active = 1
        if card_id:
            card_row=db.execute("SELECT active FROM credit_cards WHERE id=? AND user_id=?",(card_id,user_id)).fetchone()
            if not card_row:
                raise ValueError("invalid card")
            card_active=card_row["active"]
            if not card_active and not db.execute("SELECT 1 FROM expenses WHERE id=? AND user_id=? AND card_id=?",(expense_id,user_id,card_id)).fetchone():
                raise ValueError("invalid card")
        if card_id:
            card_method = db.execute("SELECT id FROM payment_methods WHERE user_id=? AND name='Kredi kartı'", (user_id,)).fetchone()
            if card_method:
                method = card_method["id"]
        start,end=month_bounds(spent_on.strftime("%Y-%m"))
        month_rows=db.execute("SELECT amount_minor FROM expenses WHERE user_id=? AND currency='TRY' AND spent_on>=? AND spent_on<?",(user_id,start,end)).fetchall()
        month_total=sum(int(r["amount_minor"]) for r in month_rows)
        if expense_id:
            existing=db.execute("SELECT amount_minor,spent_on,card_id FROM expenses WHERE id=? AND user_id=?",(expense_id,user_id)).fetchone()
            if not existing: return 0
            installment_plan=db.execute("SELECT count FROM installment_plans WHERE expense_id=? AND user_id=?", (expense_id,user_id)).fetchone()
            if installment_plan and (existing["card_id"] != card_id or existing["amount_minor"] != amount or existing["spent_on"] != spent_on.isoformat() or installments_count != installment_plan["count"]):
                raise ValueError("installment purchase details cannot change after scheduling")
            if start <= existing["spent_on"] < end:
                month_total -= int(existing["amount_minor"])
            if month_total+amount > MAX_MINOR: raise ValueError("monthly total too large")
            statement_id = _get_or_create_statement(db,user_id,card_id,spent_on,allow_inactive=not card_active) if card_id else None
            cur=db.execute("UPDATE expenses SET category_id=?,payment_method_id=?,card_id=?,statement_id=?,amount_minor=?,currency=?,spent_on=?,description=?,note=? WHERE id=? AND user_id=?",(category,method,card_id,statement_id,amount,currency,spent_on.isoformat(),description,note,expense_id,user_id))
            if not cur.rowcount: return 0
            if installments_count > 1:
                _create_installment_plan_in_db(db,user_id,expense_id,installments_count,first_installment_on)
            return expense_id
        if month_total+amount > MAX_MINOR: raise ValueError("monthly total too large")
        statement_id = _get_or_create_statement(db,user_id,card_id,spent_on) if card_id else None
        cur=db.execute("INSERT INTO expenses(user_id,category_id,payment_method_id,card_id,statement_id,amount_minor,currency,spent_on,description,note) VALUES(?,?,?,?,?,?,?,?,?,?)",(user_id,category,method,card_id,statement_id,amount,currency,spent_on.isoformat(),description,note))
        if installments_count > 1:
            _create_installment_plan_in_db(db,user_id,cur.lastrowid,installments_count,first_installment_on)
        return cur.lastrowid


def delete_expense(user_id: int, expense_id: int) -> bool:
    with connect() as db:
        row = db.execute("SELECT card_id,statement_id FROM expenses WHERE id=? AND user_id=?", (expense_id,user_id)).fetchone()
        if row and row["card_id"]:
            paid_installment = db.execute("SELECT 1 FROM installment_plans p JOIN installment_occurrences o ON o.plan_id=p.id WHERE p.expense_id=? AND p.user_id=? AND o.status='paid' LIMIT 1", (expense_id,user_id)).fetchone()
            related_payment = db.execute("SELECT 1 FROM card_repayments WHERE card_id=? AND user_id=? AND (statement_id IS NULL OR statement_id=?) LIMIT 1", (row["card_id"],user_id,row["statement_id"])).fetchone()
            if paid_installment or related_payment:
                raise ValueError("card expenses with repayment history cannot be deleted")
        cur=db.execute("DELETE FROM expenses WHERE id=? AND user_id=?",(expense_id,user_id))
        return bool(cur.rowcount)


def parse_csv_date(value):
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("invalid date")


def parse_expense_csv(user_id: int, content: str):
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("CSV dosyası çok büyük veya geçersiz.")
    if content.startswith("\ufeff"):
        content = content[1:]
    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(content, newline=""), dialect)
    try:
        headers = [h.strip().casefold() for h in next(reader)]
    except StopIteration:
        raise ValueError("CSV dosyasında başlık satırı bulunamadı.")
    aliases = {
        "tarih": "date", "date": "date", "harcama tarihi": "date",
        "tutar": "amount", "amount": "amount",
        "açıklama": "description", "aciklama": "description", "description": "description",
        "kategori": "category", "category": "category",
        "ödeme yöntemi": "payment", "odeme yontemi": "payment", "payment method": "payment",
        "not": "note", "note": "note",
    }
    columns = {}
    for i, header in enumerate(headers):
        key = aliases.get(header)
        if key and key not in columns:
            columns[key] = i
    if not all(k in columns for k in ("date", "amount", "description")):
        raise ValueError("Başlıklarda Tarih, Tutar ve Açıklama alanları bulunmalı.")
    with connect() as db:
        category_rows = db.execute("SELECT id,name FROM categories WHERE user_id=?", (user_id,)).fetchall()
        payment_rows = db.execute("SELECT id,name FROM payment_methods WHERE user_id=?", (user_id,)).fetchall()
        existing_rows = db.execute("SELECT spent_on,amount_minor,description,category_id FROM expenses WHERE user_id=?", (user_id,)).fetchall()
    categories = {r["name"].casefold(): r["id"] for r in category_rows}
    category_names = {r["id"]: r["name"] for r in category_rows}
    methods = {r["name"].casefold(): r["id"] for r in payment_rows}
    method_names = {r["id"]: r["name"] for r in payment_rows}
    existing = {(r["spent_on"], int(r["amount_minor"]), r["description"].strip().casefold(), r["category_id"]) for r in existing_rows}
    batch = set()
    results = []
    for line, cells in enumerate(reader, start=2):
        if not cells or not any(cell.strip() for cell in cells):
            continue
        row = {"row": line, "date": "", "amount_minor": None, "description": "", "category_id": None,
               "category": "", "payment_method_id": None, "payment_method": "", "note": "", "errors": [], "duplicate": False}
        try:
            row["date"] = parse_csv_date(cells[columns["date"]].strip())
            if date.fromisoformat(row["date"]) > date.today():
                raise ValueError("Gelecek tarihli harcama eklenemez.")
        except (ValueError, IndexError):
            row["errors"].append("Tarih geçersiz; YYYY-AA-GG veya GG.AA.YYYY kullanın.")
        try:
            row["amount_minor"] = money_to_minor(cells[columns["amount"]])
            if row["amount_minor"] <= 0:
                raise ValueError
        except (ValueError, IndexError):
            row["errors"].append("Tutar sıfırdan büyük, en fazla iki kuruş hanesi olmalı.")
        try:
            row["description"] = cells[columns["description"]].strip()
            if not row["description"] or len(row["description"]) > 160:
                raise ValueError
        except (ValueError, IndexError):
            row["errors"].append("Açıklama gerekli ve 160 karakteri aşmamalı.")
        for key, lookup, id_field, label_field, label in (("category", categories, "category_id", "category", "Kategori"), ("payment", methods, "payment_method_id", "payment_method", "Ödeme yöntemi")):
            if key not in columns:
                continue
            try:
                value = cells[columns[key]].strip()
                if value:
                    found = lookup.get(value.casefold())
                    if found is None:
                        raise ValueError
                    row[id_field] = found
                    row[label_field] = category_names[found] if key == "category" else method_names[found]
            except (IndexError, ValueError):
                row["errors"].append(f"{label} hesabınızdaki kategorilerle eşleşmiyor.")
        if "note" in columns:
            try:
                row["note"] = cells[columns["note"]].strip()
                if len(row["note"]) > 1000:
                    raise ValueError
            except (IndexError, ValueError):
                row["errors"].append("Not 1000 karakteri aşmamalı.")
        fingerprint = (row["date"], row["amount_minor"], row["description"].casefold(), row["category_id"])
        row["duplicate"] = not row["errors"] and (fingerprint in existing or fingerprint in batch)
        if not row["errors"]:
            batch.add(fingerprint)
        results.append(row)
    if not results:
        raise ValueError("CSV dosyasında içe aktarılacak satır bulunamadı.")
    return results


def import_csv_rows(user_id: int, rows, selected_rows):
    selected_rows = set(selected_rows)
    inserted = 0
    # Check and write under one lock so duplicate checks do not race with another import.
    with LOCK:
        fresh = parse_expense_csv(user_id, rows)
        eligible = [r for r in fresh if not r["errors"] and r["row"] in selected_rows]
        totals = {}
        for row in eligible:
            month = row["date"][:7]
            totals[month] = totals.get(month, 0) + row["amount_minor"]
        with connect() as db:
            for month, amount in totals.items():
                start, end = month_bounds(month)
                existing_total = sum(r[0] for r in db.execute("SELECT amount_minor FROM expenses WHERE user_id=? AND currency='TRY' AND spent_on>=? AND spent_on<?", (user_id, start, end)))
                if existing_total + amount > MAX_MINOR:
                    raise ValueError("Aylık harcama sınırını aşan satırlar içe aktarılamadı.")
            for row in eligible:
                db.execute("INSERT INTO expenses(user_id,category_id,payment_method_id,amount_minor,currency,spent_on,description,note) VALUES(?,?,?,?,'TRY',?,?,?)",
                           (user_id,row["category_id"],row["payment_method_id"],row["amount_minor"],row["date"],row["description"],row["note"]))
                inserted += 1
    return {"imported": inserted, "invalid_rows": [r["row"] for r in fresh if r["errors"]],
            "duplicate_rows": [r["row"] for r in fresh if r["duplicate"] and r["row"] not in selected_rows]}


def export_csv(user_id: int):
    with connect() as db:
        rows = db.execute("SELECT e.spent_on,e.amount_minor,e.description,c.name category,p.name payment_method,e.note FROM expenses e LEFT JOIN categories c ON c.id=e.category_id AND c.user_id=e.user_id LEFT JOIN payment_methods p ON p.id=e.payment_method_id AND p.user_id=e.user_id WHERE e.user_id=? ORDER BY e.spent_on,e.id", (user_id,)).fetchall()
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    def safe_cell(value):
        value = str(value or "")
        # Prevent spreadsheet applications from evaluating imported text as a formula.
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value
    for row in rows:
        amount = f"{row['amount_minor']//100},{row['amount_minor']%100:02d}"
        writer.writerow([datetime.strptime(row["spent_on"], "%Y-%m-%d").strftime("%d.%m.%Y"), amount, safe_cell(row["description"]), safe_cell(row["category"]), safe_cell(row["payment_method"]), safe_cell(row["note"])])
    return "\ufeff" + out.getvalue()


def save_budget(user_id: int, month: str, amount: str, category_id=None):
    start,_=month_bounds(month); amount_minor=money_to_minor(amount); category_id=record_id(category_id)
    if amount_minor<0 or amount_minor>MAX_MINOR: raise ValueError("invalid budget")
    with connect() as db:
        existing=db.execute("SELECT id FROM budgets WHERE user_id=? AND month=? AND category_id IS ?",(user_id,start[:7],category_id)).fetchone()
        if existing: db.execute("UPDATE budgets SET amount_minor=? WHERE id=? AND user_id=?",(amount_minor,existing["id"],user_id))
        else: db.execute("INSERT INTO budgets(user_id,month,category_id,amount_minor) VALUES(?,?,?,?)",(user_id,start[:7],category_id,amount_minor))


def recurrence_date(start_on: date, frequency: str, index: int) -> date:
    if index < 0 or frequency not in ("weekly", "monthly", "yearly"):
        raise ValueError("invalid recurrence")
    if frequency == "weekly":
        return start_on + timedelta(days=7 * index)
    if frequency == "monthly":
        month_index = start_on.year * 12 + start_on.month - 1 + index
        year, month = divmod(month_index, 12)
        month += 1
        return date(year, month, min(start_on.day, calendar.monthrange(year, month)[1]))
    year = start_on.year + index
    return date(year, start_on.month, min(start_on.day, calendar.monthrange(year, start_on.month)[1]))


def recurrence_index_on_or_after(start_on: date, frequency: str, target: date) -> int:
    if target <= start_on:
        return 0
    if frequency == "weekly":
        days = (target - start_on).days
        return (days + 6) // 7
    if frequency == "monthly":
        index = max(0, (target.year - start_on.year) * 12 + target.month - start_on.month)
    elif frequency == "yearly":
        index = max(0, target.year - start_on.year)
    else:
        raise ValueError("invalid recurrence")
    try:
        return index if recurrence_date(start_on, frequency, index) >= target else index + 1
    except (ValueError, OverflowError):
        return index


def save_recurring(user_id: int, data: dict) -> int:
    description = data.get("description", "")
    start_value = data.get("start_on", "")
    end_value = data.get("end_on") or None
    if not isinstance(description, str) or not isinstance(start_value, str) or (end_value is not None and not isinstance(end_value, str)):
        raise ValueError("invalid recurring fields")
    description = description.strip()
    start_on = date.fromisoformat(start_value)
    if start_on.isoformat() != start_value or not description or len(description) > 160:
        raise ValueError("invalid recurring expense")
    end_on = date.fromisoformat(end_value) if end_value else None
    if end_value and end_on.isoformat() != end_value:
        raise ValueError("invalid end date")
    if end_on and end_on < start_on:
        raise ValueError("end date precedes start date")
    frequency = data.get("frequency")
    if frequency not in ("weekly", "monthly", "yearly"):
        raise ValueError("invalid frequency")
    amount_minor = money_to_minor(data.get("amount"))
    if amount_minor <= 0 or amount_minor > MAX_MINOR:
        raise ValueError("invalid amount")
    category_id = record_id(data.get("category_id"))
    payment_method_id = record_id(data.get("payment_method_id"))
    recurring_id = record_id(data.get("id"))
    with connect() as db:
        if recurring_id:
            existing = db.execute("SELECT status FROM recurring_expenses WHERE id=? AND user_id=?", (recurring_id, user_id)).fetchone()
            if not existing or existing["status"] == "cancelled":
                return 0
            db.execute("UPDATE recurring_expenses SET category_id=?,payment_method_id=?,amount_minor=?,description=?,frequency=?,start_on=?,end_on=? WHERE id=? AND user_id=?",
                       (category_id,payment_method_id,amount_minor,description,frequency,start_value,end_value,recurring_id,user_id))
            return recurring_id
        cur = db.execute("INSERT INTO recurring_expenses(user_id,category_id,payment_method_id,amount_minor,description,frequency,start_on,end_on) VALUES(?,?,?,?,?,?,?,?)",
                         (user_id,category_id,payment_method_id,amount_minor,description,frequency,start_value,end_value))
        return cur.lastrowid


def set_recurring_status(user_id: int, recurring_id: int, status: str) -> bool:
    if status not in ("active", "paused", "cancelled"):
        raise ValueError("invalid status")
    with connect() as db:
        row = db.execute("SELECT status FROM recurring_expenses WHERE id=? AND user_id=?", (recurring_id,user_id)).fetchone()
        if not row:
            return False
        if row["status"] == "cancelled":
            return status == "cancelled"
        db.execute("UPDATE recurring_expenses SET status=? WHERE id=? AND user_id=?", (status,recurring_id,user_id))
        return True


def recurring_overview(user_id: int, as_of: date | None = None):
    as_of = as_of or date.today()
    horizon = as_of + timedelta(days=45)
    with connect() as db:
        rules = db.execute("SELECT r.*,c.name category,p.name payment_method FROM recurring_expenses r LEFT JOIN categories c ON c.id=r.category_id AND c.user_id=r.user_id LEFT JOIN payment_methods p ON p.id=r.payment_method_id AND p.user_id=r.user_id WHERE r.user_id=? ORDER BY r.status,r.description", (user_id,)).fetchall()
        paid = {}
        for row in rules:
            paid[row["id"]] = {r[0] for r in db.execute("SELECT scheduled_on FROM recurring_occurrences WHERE recurring_id=? AND user_id=?", (row["id"],user_id))}
    rule_list=[]
    upcoming=[]
    for raw in rules:
        rule=dict(raw)
        start_on=date.fromisoformat(rule["start_on"])
        end_on=date.fromisoformat(rule["end_on"]) if rule["end_on"] else None
        next_on=None
        rule_upcoming=[]
        if rule["status"] != "cancelled":
            index=recurrence_index_on_or_after(start_on,rule["frequency"],as_of)
            for _ in range(len(paid[rule["id"]]) + 2):
                try: occurrence=recurrence_date(start_on,rule["frequency"],index)
                except (ValueError,OverflowError): break
                if end_on and occurrence>end_on: break
                occurrence_text=occurrence.isoformat()
                if occurrence_text not in paid[rule["id"]]:
                    if next_on is None: next_on=occurrence_text
                    if rule["status"] == "active" and occurrence <= horizon:
                        item={"recurring_id":rule["id"],"scheduled_on":occurrence_text,"description":rule["description"],"amount_minor":rule["amount_minor"],"category":rule["category"] or "Kategorisiz","frequency":rule["frequency"]}
                        rule_upcoming.append(item); upcoming.append(item)
                if occurrence > horizon and next_on:
                    break
                index += 1
                if len(rule_upcoming)>=6: break
        rule["next_on"]=next_on
        rule["upcoming"]=rule_upcoming
        rule_list.append(rule)
    upcoming.sort(key=lambda item:(item["scheduled_on"],item["description"]))
    return {"rules":rule_list,"upcoming":upcoming}


def complete_recurring_occurrence(user_id: int, recurring_id: int, scheduled_value: str, as_of: date | None = None):
    as_of = as_of or date.today()
    try:
        scheduled_on=date.fromisoformat(scheduled_value)
    except (ValueError,TypeError):
        raise ValueError("invalid occurrence date")
    if scheduled_on.isoformat()!=scheduled_value:
        raise ValueError("invalid occurrence date")
    with LOCK, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rule=db.execute("SELECT * FROM recurring_expenses WHERE id=? AND user_id=?",(recurring_id,user_id)).fetchone()
        if not rule:return None
        if rule["status"]!="active":raise ValueError("recurring rule is not active")
        start_on=date.fromisoformat(rule["start_on"])
        if scheduled_on<start_on or recurrence_date(start_on,rule["frequency"],recurrence_index_on_or_after(start_on,rule["frequency"],scheduled_on))!=scheduled_on:
            raise ValueError("date is not a scheduled occurrence")
        if rule["end_on"] and scheduled_on>date.fromisoformat(rule["end_on"]):
            raise ValueError("occurrence is after rule end")
        previous=db.execute("SELECT expense_id FROM recurring_occurrences WHERE recurring_id=? AND user_id=? AND scheduled_on=?",(recurring_id,user_id,scheduled_value)).fetchone()
        if previous:return {"expense_id":previous["expense_id"],"created":False}
        paid_on=as_of.isoformat()
        month=paid_on[:7]
        month_start,month_end=month_bounds(month)
        total=sum(int(row[0]) for row in db.execute("SELECT amount_minor FROM expenses WHERE user_id=? AND currency='TRY' AND spent_on>=? AND spent_on<?",(user_id,month_start,month_end)))
        if total+int(rule["amount_minor"])>MAX_MINOR:raise ValueError("monthly total too large")
        cur=db.execute("INSERT INTO expenses(user_id,category_id,payment_method_id,amount_minor,currency,spent_on,description,note) VALUES(?,?,?,?,'TRY',?,?,?)",
                       (user_id,rule["category_id"],rule["payment_method_id"],rule["amount_minor"],paid_on,rule["description"],"Düzenli harcama"))
        expense_id=cur.lastrowid
        db.execute("INSERT INTO recurring_occurrences(recurring_id,user_id,scheduled_on,expense_id,paid_on) VALUES(?,?,?,?,?)",(recurring_id,user_id,scheduled_value,expense_id,paid_on))
        return {"expense_id":expense_id,"created":True}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        production = os.environ.get("APP_ENV", "development").strip().lower() == "production"
        backup_command = argv == ["backup"] or argv == ["backup", "--encrypted"]
        if argv[:2] in (["invite", "create"], ["invite", "revoke"]):
            if production:
                validate_runtime_configuration()
            else:
                init_db()
            if not private_beta_enabled(strict=production):
                raise ValueError("Invitation commands require PRIVATE_BETA=1.")
            if argv[:2] == ["invite", "create"]:
                hours = 72
                if len(argv) == 4 and argv[2] == "--expires-hours":
                    hours = int(argv[3])
                elif len(argv) != 2:
                    raise ValueError("Usage: python3 app.py invite create [--expires-hours 1..720]")
                invitation_id, token = create_invitation(hours)
                print(f"Invitation {invitation_id} (expires in {hours} hours). Copy this token now; it is shown only once:\n{token}")
                return 0
            if len(argv) != 3:
                raise ValueError("Usage: python3 app.py invite revoke INVITATION_ID")
            print("Invitation revoked." if revoke_invitation(argv[2]) else "Invitation could not be revoked.")
            return 0
        if production and (backup_command or (len(argv) == 2 and argv[0] == "restore")):
            validate_runtime_configuration()
        if backup_command:
            print(f"Backup created: {create_backup(encrypted=production or len(argv) == 2)}")
            return 0
        if len(argv) == 2 and argv[0] == "restore":
            print("WARNING: Stop the app before restoring. An existing valid database will be saved as a recovery backup first.", file=sys.stderr)
            result = restore_backup(argv[1])
            print(f"Restored database from: {result['restored_from']}")
            if result["recovery_backup"]:
                print(f"Pre-restore recovery backup: {result['recovery_backup']}")
            else:
                print("No prior database existed; no pre-restore recovery backup was needed.")
            return 0
        if argv:
            print("Usage: python3 app.py [backup [--encrypted] | restore BACKUP_FILE | invite create [--expires-hours 1..720] | invite revoke INVITATION_ID]", file=sys.stderr)
            return 2
        if os.environ.get("APP_ENV", "development").strip().lower() == "production":
            raise ValueError("The built-in HTTP server is development-only. Configure a production WSGI/ASGI server and HTTPS before starting in production.")
        from webapp import create_app
        from werkzeug.serving import run_simple
        host=os.environ.get("APP_HOST","127.0.0.1"); port=int(os.environ.get("APP_PORT","8000"))
        print(f"Expense tracker development server listening on http://{host}:{port}")
        run_simple(host,port,create_app(),use_reloader=False,threaded=True)
        return 0
    except (OSError, sqlite3.DatabaseError, ValueError) as error:
        print(f"Operation failed: {error}", file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
