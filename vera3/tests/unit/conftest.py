"""Test-time env defaults for modules that read env at import.

Lives in unit/conftest.py so it runs BEFORE any test-module import,
keeping the test files themselves clean of stdlib/os env mutation
between import statements (which ruff I001 flags as broken ordering).
"""
import os

os.environ.setdefault("INTERNAL_SECRET", "test-internal-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TOKEN_SECRET", "0" * 44)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
