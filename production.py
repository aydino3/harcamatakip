"""Waitress production entry point for one persistent SQLite instance."""
from __future__ import annotations

import os

if os.environ.get("APP_ENV", "").strip().lower() != "production":
    raise SystemExit("Set APP_ENV=production to use the production server entry point.")

port_value = os.environ.get("PORT", "10000")
if not port_value.isdigit() or not 1 <= int(port_value) <= 65535:
    raise SystemExit("PORT must be a valid TCP port number.")
thread_value = os.environ.get("WEB_CONCURRENCY", "4")
if not thread_value.isdigit() or not 1 <= int(thread_value) <= 16:
    raise SystemExit("WEB_CONCURRENCY must be an integer from 1 through 16.")

from waitress import serve
from wsgi import app

options = {"host": "0.0.0.0", "port": int(port_value), "threads": int(thread_value)}
serve(app, **options)
