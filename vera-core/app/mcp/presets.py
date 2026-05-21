"""Curated MCP server presets — one-click registration from the dashboard.

A preset is a recipe (command + required env vars). The user picks one,
the dashboard inserts a row into mcp_servers and the manager starts it.
"""

PRESETS: list[dict] = [
    {
        "id": "gmail",
        "label": "Gmail (gongrzhe)",
        "description": "Google Mail via OAuth. Replaces the in-tree vera-gmail poller.",
        "transport": "stdio",
        "command": ["npx", "-y", "@gongrzhe/server-gmail-autoauth-mcp"],
        "env_required": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
        "notes": (
            "Run `npx @gongrzhe/server-gmail-autoauth-mcp auth` once locally "
            "to obtain the refresh token, then paste credentials here."
        ),
    },
    {
        "id": "telegram",
        "label": "Telegram (chigwell)",
        "description": "Telethon-backed Telegram MCP. Shares /data/sessions/userbot.session.",
        "transport": "stdio",
        "command": ["npx", "-y", "@chigwell/telegram-mcp"],
        "env_required": ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_PATH"],
        "notes": "TELEGRAM_SESSION_PATH defaults to /data/sessions/userbot.session.",
    },
    {
        "id": "fetch",
        "label": "Web fetch (modelcontextprotocol)",
        "description": "Read web pages as Markdown. Replaces ad-hoc curl/httpx scraping.",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-fetch"],
        "env_required": [],
        "notes": "No credentials required.",
    },
    {
        "id": "git",
        "label": "Git (modelcontextprotocol)",
        "description": "Git operations against /repos. Replaces vera-git bot.",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-git", "--repository", "/repos"],
        "env_required": [],
        "notes": "Mount the repo at /repos. GitHub auth via /repos/.git/config credential helper.",
    },
]


def find(preset_id: str) -> dict | None:
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None
