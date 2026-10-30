"""Local-only privacy preview, backed by an explicitly supplied synthetic DB."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webapp

if __name__ == '__main__':
    if os.environ.get('APP_ENV', 'development') != 'development' or os.environ.get('VERCEL'):
        raise SystemExit('Bu önizleme yalnız yerelde çalışır.')
    if not os.environ.get('EXPENSE_DB_PATH') or any(os.environ.get(key) for key in ('EXPENSE_DATABASE_URL', 'DATABASE_URL', 'POSTGRES_URL')):
        raise SystemExit('Önizleme için ayrı bir yerel SQLite dosyası gerekir.')
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'privacy/notice.draft.json'
    notice = json.loads(source.read_text(encoding='utf-8'))
    application = webapp.create_app({'PRIVACY_NOTICE': notice, 'PRIVACY_NOTICE_ALLOW_DRAFT': True})
    application.run(host='127.0.0.1', port=int(os.environ.get('APP_PORT', '61507')), debug=False)
