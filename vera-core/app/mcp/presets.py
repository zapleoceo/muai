"""Curated MCP server presets — one-click registration from the dashboard.

A preset is a recipe (command + required env vars). The user picks one,
the dashboard inserts a row into mcp_servers and the manager starts it.
"""

PRESETS: list[dict] = [
    {
        "id": "fetch",
        "label": "Web fetch (mcp-server-fetch)",
        "description": "Read web pages as Markdown. Replaces ad-hoc curl/httpx scraping.",
        "transport": "stdio",
        "command": ["uvx", "mcp-server-fetch"],
        "env_required": [],
        "notes": "No credentials required. Installed lazily via uvx on first run.",
    },
    {
        "id": "git",
        "label": "Git (mcp-server-git)",
        "description": "Git operations against a mounted repo. Replaces vera-git bot.",
        "transport": "stdio",
        "command": ["uvx", "mcp-server-git", "--repository", "/var/www/vera"],
        "env_required": [],
        "notes": "Repo must be mounted into vera-core. GitHub auth via repo's git credential helper.",
    },
    {
        "id": "filesystem",
        "label": "Filesystem (modelcontextprotocol)",
        "description": "Read/write files in a sandboxed directory.",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"],
        "env_required": [],
        "notes": "Scoped to /data inside the container.",
    },
    {
        "id": "memory",
        "label": "Knowledge graph memory (modelcontextprotocol)",
        "description": "Lightweight persistent memory graph for Vera.",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-memory"],
        "env_required": [],
        "notes": "Stores graph in process memory; survives only while server runs.",
    },
    {
        "id": "github",
        "label": "GitHub (modelcontextprotocol)",
        "description": "GitHub API tools (issues, PRs, code search). Replaces vera-git bot.",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
        "env_required": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "notes": "Token needs `repo` scope. Create at https://github.com/settings/tokens",
    },
]


def find(preset_id: str) -> dict | None:
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None
