import os

os.environ.setdefault("APP_ENV", "production")
os.environ.setdefault("VERCEL", "1")
os.environ.setdefault("PRIVATE_BETA", "0")
os.environ.setdefault("COOKIE_SECURE", "1")
os.environ.setdefault("TRUST_PROXY", "1")
os.environ.setdefault("EXPENSE_DB_PATH", "/tmp/expense-tracker.sqlite3")
os.environ.setdefault("EXPENSE_BACKUP_DIR", "/tmp/expense-tracker-backups")

from webapp import create_app

app = create_app()
