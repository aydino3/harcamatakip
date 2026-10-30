import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that `webapp` and `app` resolve
# correctly regardless of which directory Vercel invokes the function from.
_HERE = Path(__file__).resolve().parent          # .../api/
_ROOT = _HERE.parent                             # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("APP_ENV", "production")
os.environ.setdefault("VERCEL", "1")
os.environ.setdefault("PRIVATE_BETA", "0")
os.environ.setdefault("COOKIE_SECURE", "1")
os.environ.setdefault("TRUST_PROXY", "1")
os.environ.setdefault("EXPENSE_DB_PATH", "/tmp/expense-tracker.sqlite3")
os.environ.setdefault("EXPENSE_BACKUP_DIR", "/tmp/expense-tracker-backups")

# Operator explicitly approved publishing the notice as a draft on 2026-10-30.
# This setting does not certify legal compliance or an international transfer basis.
os.environ.setdefault("PRIVACY_NOTICE_PATH", str(_ROOT / "privacy" / "notice.draft.json"))
os.environ.setdefault("PRIVACY_NOTICE_ALLOW_DRAFT", "1")

from webapp import create_app  # noqa: E402

app = create_app()
