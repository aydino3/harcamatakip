"""Production WSGI entry point. Configuration and database are validated at import."""
import os

if os.environ.get("APP_ENV", "").strip().lower() != "production":
    raise RuntimeError("Set APP_ENV=production before importing the production WSGI entry point.")

from webapp import create_app

app = create_app()
