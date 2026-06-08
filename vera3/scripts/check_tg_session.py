"""Сheck Telethon session file integrity."""
import sqlite3

path = "/sessions/userbot.session"
c = sqlite3.connect(path)
print("tables:", [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")])
for t in c.execute("SELECT * FROM version"):
    print("version row:", t)
schema = c.execute("SELECT sql FROM sqlite_master WHERE name='version'").fetchone()
print("schema:", schema)

from telethon.sessions import SQLiteSession
try:
    s = SQLiteSession(path)
    print("✓ Telethon can load this session")
    print("  saved DC:", s.dc_id, "auth_key set:", bool(s.auth_key))
except Exception as e:
    print(f"✗ Telethon load failed: {e}")
