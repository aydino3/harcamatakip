"""PostgreSQL compatibility wrapper — makes psycopg2 look like sqlite3 to app.py.

When DATABASE_URL is set the wrapper:
  - translates ? placeholders to %s
  - translates SQLite datetime() / date() calls to text-formatted PostgreSQL equivalents
    so that TEXT columns storing ISO timestamps compare correctly
  - translates INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
  - translates IS ? → IS NOT DISTINCT FROM %s (for nullable column comparisons)
  - injects RETURNING id into INSERT statements to emulate .lastrowid
  - skips PRAGMA and BEGIN IMMEDIATE statements
  - provides executescript() by splitting on ';' and executing each statement
  - provides dict-like row access via RealDictCursor
  - provides a context manager that commits/rolls back like sqlite3
"""
from __future__ import annotations

import re
import os


# ---------------------------------------------------------------------------
# SQL translation helpers
# ---------------------------------------------------------------------------

# SQLite datetime functions → PostgreSQL text-formatted equivalents.
# We keep output as TEXT (via TO_CHAR) so comparisons with TEXT columns work.
_DATETIME_NOW_INTERVAL = re.compile(
    r"datetime\(\s*'now'\s*,\s*'([+-])(\d+)\s+(\w+)'\s*\)",
    re.IGNORECASE,
)
_DATETIME_NOW_PLAIN = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_DATE_NOW = re.compile(r"date\(\s*'now'\s*\)", re.IGNORECASE)

# IS ? → IS NOT DISTINCT FROM ? (handles NULL equality in PostgreSQL)
_IS_PARAM = re.compile(r'\bIS\s+\?', re.IGNORECASE)

# SQLite-only statement prefixes to silently skip
_SKIP_PREFIXES = ("pragma ", "begin immediate", "begin exclusive")

# Map SQLite interval unit aliases to PostgreSQL
_UNIT_MAP = {
    "day": "days", "days": "days",
    "hour": "hours", "hours": "hours",
    "minute": "minutes", "minutes": "minutes",
    "month": "months", "months": "months",
    "year": "years", "years": "years",
}

# PostgreSQL text format matching SQLite's datetime('now') output
_PG_NOW_TEXT = "TO_CHAR(timezone('utc', NOW()), 'YYYY-MM-DD HH24:MI:SS')"
_PG_DATE_TEXT = "TO_CHAR(timezone('utc', NOW()), 'YYYY-MM-DD')"


def _translate_sql(sql: str) -> str | None:
    """Return the PostgreSQL-equivalent SQL string, or None to skip the statement."""
    stripped = sql.strip()
    if not stripped:
        return None
    lower = stripped.lower()
    for prefix in _SKIP_PREFIXES:
        if lower.startswith(prefix):
            return None

    # --- datetime translations (must happen before ? replacement) ---

    def _replace_interval(m):
        sign, n, unit = m.group(1), m.group(2), m.group(3).lower()
        pg_unit = _UNIT_MAP.get(unit, unit)
        op = "+" if sign == "+" else "-"
        return f"TO_CHAR(timezone('utc', NOW()) {op} INTERVAL '{n} {pg_unit}', 'YYYY-MM-DD HH24:MI:SS')"

    sql = _DATETIME_NOW_INTERVAL.sub(_replace_interval, sql)
    sql = _DATETIME_NOW_PLAIN.sub(_PG_NOW_TEXT, sql)
    sql = _DATE_NOW.sub(_PG_DATE_TEXT, sql)

    # --- IS ? → IS NOT DISTINCT FROM ? (before ? → %s translation) ---
    sql = _IS_PARAM.sub('IS NOT DISTINCT FROM ?', sql)

    # --- INSERT OR IGNORE / INSERT OR REPLACE → track for ON CONFLICT DO NOTHING ---
    sql = re.sub(r'\bINSERT\s+OR\s+IGNORE\b', 'INSERT /*OR_IGNORE*/', sql, flags=re.IGNORECASE)
    sql = re.sub(r'\bINSERT\s+OR\s+REPLACE\b', 'INSERT /*OR_IGNORE*/', sql, flags=re.IGNORECASE)

    # --- ? placeholders → %s ---
    sql = _translate_placeholders(sql)

    return sql


def _translate_placeholders(sql: str) -> str:
    """Replace bare ? with %s, skipping those inside string literals."""
    result = []
    in_single = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_single:
            in_single = True
            result.append(ch)
        elif ch == "'" and in_single:
            # handle escaped single quote ''
            if i + 1 < len(sql) and sql[i + 1] == "'":
                result.append("''")
                i += 2
                continue
            in_single = False
            result.append(ch)
        elif ch == "?" and not in_single:
            result.append("%s")
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _inject_returning(sql: str) -> tuple[str, bool]:
    """Append RETURNING id to INSERT statements (for lastrowid emulation).

    If the statement was INSERT OR IGNORE, also appends ON CONFLICT DO NOTHING
    before RETURNING so that conflicts are silently skipped and lastrowid is None.
    """
    stripped_lower = sql.strip().lower()
    if not stripped_lower.startswith("insert"):
        return sql, False
    if "returning" in stripped_lower:
        return sql, False

    base = sql.rstrip().rstrip(";")
    is_or_ignore = "/*or_ignore*/" in base.lower()
    # Remove the marker comment
    base = re.sub(r'/\*OR_IGNORE\*/', '', base, flags=re.IGNORECASE).rstrip()

    if is_or_ignore and "on conflict" not in base.lower():
        base += " ON CONFLICT DO NOTHING"

    return base + " RETURNING id", True


# ---------------------------------------------------------------------------
# Row wrapper — makes psycopg2 RealDictRow subscriptable by index too
# ---------------------------------------------------------------------------

class _Row(dict):
    """Dict subclass that also supports positional indexing like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


# ---------------------------------------------------------------------------
# Cursor wrapper
# ---------------------------------------------------------------------------

class _PgCursor:
    def __init__(self, raw_cursor):
        self._cur = raw_cursor
        self._lastrowid: int | None = None
        self.rowcount: int = 0

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    def execute(self, sql: str, params=()) -> "_PgCursor":
        translated = _translate_sql(sql)
        if translated is None:
            self.rowcount = 0
            return self
        translated, has_returning = _inject_returning(translated)
        self._cur.execute(translated, params or ())
        self.rowcount = self._cur.rowcount if self._cur.rowcount != -1 else 0
        if has_returning:
            row = self._cur.fetchone()
            self._lastrowid = row["id"] if row else None
            self.rowcount = 1 if row else 0
        return self

    def executemany(self, sql: str, seq_of_params) -> "_PgCursor":
        translated = _translate_sql(sql)
        if translated is None:
            return self
        translated, has_returning = _inject_returning(translated)
        if has_returning:
            for params in seq_of_params:
                self._cur.execute(translated, params)
                row = self._cur.fetchone()
                if row:
                    self._lastrowid = row["id"]
        else:
            from psycopg2.extras import execute_batch
            execute_batch(self._cur, translated, list(seq_of_params))
            self.rowcount = self._cur.rowcount
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _Row(row)

    def fetchall(self):
        return [_Row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        for row in self._cur:
            yield _Row(row)


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class PgConnection:
    """Thin sqlite3-compatible wrapper around a psycopg2 connection."""

    def __init__(self, raw_conn):
        self._conn = raw_conn
        from psycopg2.extras import RealDictCursor
        self._cursor_factory = RealDictCursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def in_transaction(self):
        # psycopg2 status: 1 = IDLE (no active transaction)
        try:
            return self._conn.status != 1
        except Exception:
            return False

    def _new_cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor(cursor_factory=self._cursor_factory))

    def execute(self, sql: str, params=()) -> _PgCursor:
        cur = self._new_cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq_of_params) -> _PgCursor:
        cur = self._new_cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def executescript(self, script: str):
        """Execute a multi-statement SQL script (CREATE TABLE IF NOT EXISTS etc.)."""
        statements = _split_script(script)
        cur = self._new_cursor()
        for stmt in statements:
            translated = _translate_sql(stmt)
            if translated:
                try:
                    cur._cur.execute(translated)
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
        return cur


# ---------------------------------------------------------------------------
# Script splitter (used by executescript and _apply_pg_schema)
# ---------------------------------------------------------------------------

def _split_script(script: str) -> list[str]:
    """Split a SQL script on semicolons, skipping those inside string literals."""
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    for ch in script:
        if ch == "'" and not in_single:
            in_single = True
            current.append(ch)
        elif ch == "'" and in_single:
            in_single = False
            current.append(ch)
        elif ch == ";" and not in_single:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
        else:
            current.append(ch)
    last = "".join(current).strip()
    if last:
        statements.append(last)
    return statements


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def connect_pg(database_url: str) -> PgConnection:
    """Open a psycopg2 connection wrapped for sqlite3 compatibility.

    Supabase requires SSL; we append sslmode=require if not already present.
    """
    import psycopg2
    # Supabase (and most managed PostgreSQL) requires SSL.
    if "sslmode" not in database_url:
        sep = "&" if "?" in database_url else "?"
        database_url = database_url + sep + "sslmode=require"
    try:
        conn = psycopg2.connect(database_url, connect_timeout=10)
    except Exception as exc:
        raise RuntimeError(f"PostgreSQL bağlantısı kurulamadı: {exc}") from exc
    conn.autocommit = False
    return PgConnection(conn)
