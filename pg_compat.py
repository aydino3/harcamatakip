"""PostgreSQL compatibility wrapper — makes psycopg2 look like sqlite3 to app.py.

When DATABASE_URL is set the wrapper:
  - translates ? placeholders to %s
  - translates SQLite datetime() / date() calls to text-formatted PostgreSQL equivalents
    so that TEXT columns storing ISO timestamps compare correctly
  - translates INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
  - translates IS ? → IS NOT DISTINCT FROM %s (for nullable column comparisons)
  - injects RETURNING for single integer primary keys into INSERT statements to emulate .lastrowid
  - skips PRAGMA and BEGIN IMMEDIATE statements
  - provides executescript() by splitting on ';' and executing each statement
  - provides dict-like row access via RealDictCursor
  - provides a context manager that commits/rolls back like sqlite3
"""
from __future__ import annotations

import re
import os


class DatabaseConnectionError(RuntimeError):
    """Connection failure without credentials or a private DSN in its text."""


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


# Mask SQL literals, quoted identifiers and comments before looking for clauses.
# Keyword-like text inside a value is data, not an SQL clause.
_SQL_NON_CODE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/|(?P<dollar>\$[A-Za-z_0-9]*\$).*?(?P=dollar)", re.DOTALL)
_IDENTIFIER = r'(?:"(?:""|[^"])+"|[A-Za-z_][A-Za-z_0-9$]*)'
_INSERT_TABLE = re.compile(r'^\s*INSERT\s+(?:/\*OR_IGNORE\*/\s*)?INTO\s+(' + _IDENTIFIER + r'(?:\s*\.\s*' + _IDENTIFIER + r')?)', re.IGNORECASE)


def _sql_code(sql: str) -> str:
    return _SQL_NON_CODE.sub(lambda match: " " * len(match.group()), sql)


def _insert_table(sql: str) -> str | None:
    match = _INSERT_TABLE.match(sql)
    return match.group(1) if match else None


def _inject_returning(sql: str, identity_column: str | None) -> tuple[str, str | None]:
    """Translate conflict handling and emulate lastrowid only for an actual key.

    Explicit RETURNING stays available to the caller; it must not be consumed.
    A table without a single integer primary key needs no synthetic RETURNING.
    """
    if not _insert_table(sql):
        return sql, None
    base = sql.rstrip().rstrip(";")
    is_or_ignore = bool(re.search(r'/\*OR_IGNORE\*/', base, re.IGNORECASE))
    base = re.sub(r'/\*OR_IGNORE\*/', '', base, flags=re.IGNORECASE).rstrip()
    code = _sql_code(base)
    returning = re.search(r'\bRETURNING\b', code, re.IGNORECASE)
    if is_or_ignore and not re.search(r'\bON\s+CONFLICT\b', code, re.IGNORECASE):
        position = returning.start() if returning else len(base)
        base = base[:position].rstrip() + " ON CONFLICT DO NOTHING " + base[position:]
    if returning or identity_column is None:
        return base.rstrip(), None
    quoted_column = '"' + identity_column.replace('"', '""') + '"'
    return base.rstrip() + " RETURNING " + quoted_column, identity_column


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
    def __init__(self, raw_cursor, identity_resolver):
        self._cur = raw_cursor
        self._identity_resolver = identity_resolver
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
        table = _insert_table(translated)
        identity = self._identity_resolver(table) if table and not re.search(r"\bRETURNING\b", _sql_code(translated), re.IGNORECASE) else None
        translated, returned_column = _inject_returning(translated, identity)
        self._cur.execute(translated, params or ())
        self.rowcount = self._cur.rowcount if self._cur.rowcount != -1 else 0
        self._lastrowid = None
        if returned_column:
            row = self._cur.fetchone()
            self._lastrowid = row[returned_column] if row else None
            self.rowcount = 1 if row else 0
        return self

    def executemany(self, sql: str, seq_of_params) -> "_PgCursor":
        translated = _translate_sql(sql)
        if translated is None:
            return self
        table = _insert_table(translated)
        identity = self._identity_resolver(table) if table and not re.search(r"\bRETURNING\b", _sql_code(translated), re.IGNORECASE) else None
        translated, returned_column = _inject_returning(translated, identity)
        self.rowcount = 0
        self._lastrowid = None
        if returned_column:
            for params in seq_of_params:
                self._cur.execute(translated, params)
                row = self._cur.fetchone()
                if row:
                    self._lastrowid = row[returned_column]
                    self.rowcount += 1
        else:
            self._cur.executemany(translated, seq_of_params)
            self.rowcount = max(self._cur.rowcount, 0)
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
        self._identity_columns = {}

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
        self._identity_columns.clear()

    def close(self):
        self._conn.close()

    @property
    def in_transaction(self):
        # psycopg2 status: 1 = IDLE (no active transaction)
        try:
            return self._conn.status != 1
        except Exception:
            return False

    def _integer_primary_key(self, relation: str) -> str | None:
        # Cache per connection/transaction, never across users or schemas.
        if relation not in self._identity_columns:
            with self._conn.cursor() as metadata:
                metadata.execute(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0] "
                    "WHERE i.indrelid=to_regclass(%s) AND i.indisprimary AND i.indnkeyatts=1 "
                    "AND a.atttypid IN ('smallint'::regtype,'integer'::regtype,'bigint'::regtype)",
                    (relation,),
                )
                row = metadata.fetchone()
            self._identity_columns[relation] = row[0] if row else None
        return self._identity_columns[relation]

    def _new_cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor(cursor_factory=self._cursor_factory), self._integer_primary_key)

    def execute(self, sql: str, params=()) -> _PgCursor:
        cur = self._new_cursor()
        cur.execute(sql, params)
        if re.match(r"\s*(CREATE|ALTER|DROP)\b", sql, re.IGNORECASE):
            self._identity_columns.clear()
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

def _pg_connect_kwargs(database_url: str) -> dict:
    """Parse a PostgreSQL URL into psycopg2 keyword arguments.

    Forces IPv4 resolution via socket.AF_INET + psycopg2's ``hostaddr`` so that
    Vercel serverless functions (which cannot make outbound IPv6 connections) can
    reach Supabase even when DNS returns an AAAA (IPv6) record first.
    """
    import socket
    from urllib.parse import urlparse, unquote, parse_qs

    parsed = urlparse(database_url)
    hostname = parsed.hostname or "localhost"
    port = parsed.port or 5432
    username = parsed.username or "postgres"
    password = unquote(parsed.password) if parsed.password else ""
    dbname = (parsed.path or "/postgres").lstrip("/") or "postgres"

    qs = parse_qs(parsed.query)
    sslmode = qs.get("sslmode", ["require"])[0]

    kwargs: dict = {
        "host": hostname,          # kept for SSL certificate hostname validation
        "port": port,
        "user": username,
        "password": password,
        "dbname": dbname,
        "sslmode": sslmode,
        "connect_timeout": 10,
    }

    # Vercel Lambda cannot make outbound IPv6 connections.
    # Resolve the hostname exclusively to IPv4 (AF_INET) and pass the result as
    # `hostaddr` so psycopg2 connects to the IPv4 address while still sending
    # the SNI hostname for SSL certificate verification.
    try:
        infos = socket.getaddrinfo(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        if infos:
            ipv4_addr = infos[0][4][0]
            kwargs["hostaddr"] = ipv4_addr
    except socket.gaierror:
        pass  # no IPv4 record found — let psycopg2 try normally

    return kwargs


def connect_pg(database_url: str) -> PgConnection:
    """Open a psycopg2 connection wrapped for sqlite3 compatibility.

    Supabase requires SSL and Vercel does not support IPv6 outbound connections.
    Both constraints are handled here: sslmode=require is injected if absent,
    and the hostname is resolved to an IPv4 address via ``hostaddr``.
    """
    import psycopg2
    try:
        kwargs = _pg_connect_kwargs(database_url)
        conn = psycopg2.connect(**kwargs)
    except Exception as exc:
        raise DatabaseConnectionError("PostgreSQL bağlantısı kurulamadı.") from exc
    conn.autocommit = False
    return PgConnection(conn)
