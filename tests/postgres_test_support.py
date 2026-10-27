"""Local-only PostgreSQL fixtures. Never accept an application database URL."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import selectors
from urllib.parse import quote, urlsplit, parse_qs


def validate_test_admin_dsn(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError("PostgreSQL testi için geçerli bir bağlantı gerekli.")
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("PostgreSQL testleri yalnızca yerel sunucuya bağlanabilir.")
    if not parsed.path.lstrip("/").startswith("finance_test_"):
        raise ValueError("Test veritabanının adı finance_test_ ile başlamalı.")
    if parsed.fragment or set(parse_qs(parsed.query)) - {"sslmode"}:
        raise ValueError("Test bağlantısı sunucu veya veritabanını değiştiren seçenek içeremez.")
    return value


class LocalPostgresCluster:
    def __init__(self):
        self.directory = None
        self.bin_dir = None
        self.data_dir = None
        self.admin_dsn = None

    def start(self):
        supplied = os.environ.get("FINANCE_TEST_POSTGRES_ADMIN_DSN")
        if supplied:
            self.admin_dsn = validate_test_admin_dsn(supplied)
            return self
        self.bin_dir = Path(os.environ["FINANCE_TEST_POSTGRES_BIN_DIR"]).resolve()
        for program in ("initdb", "pg_ctl"):
            if not (self.bin_dir / program).is_file():
                raise ValueError("Yerel PostgreSQL test araçları bulunamadı.")
        self.directory = tempfile.TemporaryDirectory(prefix="finance-pg-", dir="/tmp")
        root = Path(self.directory.name)
        self.data_dir = root / "data"
        password = secrets.token_hex(24)
        password_file = root / "initial-password"
        password_file.write_text(password + "\n")
        password_file.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        try:
            subprocess.run(
                [str(self.bin_dir / "initdb"), "-D", str(self.data_dir), "-U", "finance_test",
                 "--pwfile", str(password_file), "--auth-local=trust", "--auth-host=scram-sha-256",
                 "--no-locale", "--encoding=UTF8"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            password_file.unlink()
            subprocess.run(
                [str(self.bin_dir / "pg_ctl"), "-D", str(self.data_dir), "-l", str(root / "server.log"),
                 "-o", f"-h 127.0.0.1 -p {port} -k {root} -F -c shared_buffers=16MB -c max_connections=40",
                 "-w", "-t", "20", "start"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            # This administrative database is created below using the local default database.
            self.admin_dsn = f"postgresql://finance_test:{quote(password)}@127.0.0.1:{port}/finance_test_admin?sslmode=disable"
            import psycopg2
            from psycopg2 import sql
            connection = psycopg2.connect(self.admin_dsn.replace("/finance_test_admin?", "/postgres?"))
            try:
                connection.autocommit = True
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("finance_test_admin")))
            finally:
                connection.close()
            validate_test_admin_dsn(self.admin_dsn)
            return self
        except subprocess.CalledProcessError as exc:
            self.close()
            raise RuntimeError(exc.stderr.decode(errors="replace")) from exc
        except Exception:
            self.close()
            raise

    def close(self):
        if self.data_dir and (self.data_dir / "postmaster.pid").exists():
            subprocess.run(
                [str(self.bin_dir / "pg_ctl"), "-D", str(self.data_dir), "-m", "fast", "-w", "stop"],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        if self.directory:
            self.directory.cleanup()
            self.directory = None


def postgres_tests_enabled() -> bool:
    return bool(os.environ.get("FINANCE_TEST_POSTGRES_BIN_DIR") or os.environ.get("FINANCE_TEST_POSTGRES_ADMIN_DSN") or os.environ.get("FINANCE_TEST_PGLITE_SERVER"))


class LocalPGliteDatabase:
    """Optional isolated WASM PostgreSQL; not a concurrency/production substitute."""
    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        script = Path(os.environ["FINANCE_TEST_PGLITE_SERVER"]).resolve()
        self.process = subprocess.Popen(["node", str(script), str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=30) or self.process.stdout.readline().strip() != b"READY":
                self.close()
                raise RuntimeError("Yalıtılmış PGlite sunucusu başlatılamadı.")
        finally:
            selector.close()
        self.dsn = f"postgresql://postgres@127.0.0.1:{port}/finance_test_wasm?sslmode=disable"
        validate_test_admin_dsn(self.dsn)
        return self

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
