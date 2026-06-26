"""Token utilities — Fernet encrypt/decrypt for OAuth/session secrets.

LLM-ключи теперь живут в брокере (см. vera_shared.llm). Здесь остался
только crypto-хелпер для шифрования Gmail OAuth refresh-токенов,
Instagram sessionid и Telegram userbot session-файлов в БД.
"""
from vera_shared.tokens.crypto import decrypt, encrypt

__all__ = ["encrypt", "decrypt"]
