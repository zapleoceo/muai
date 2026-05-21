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
        "label": "Filesystem — /data (modelcontextprotocol)",
        "description": "Read/write files in /data (sessions, vera.db).",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/data"],
        "env_required": [],
        "notes": "Scoped to /data inside the container.",
    },
    {
        "id": "docs",
        "label": "Project docs (read-only)",
        "description": (
            "Read VERA.md, SELF_EXTENSION.md and any other markdown docs in "
            "/var/www/vera/docs and the repo root. Lets Vera answer "
            "questions about her own architecture from the actual files."
        ),
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem",
                    "/var/www/vera/docs", "/var/www/vera/VERA.md",
                    "/var/www/vera/CLAUDE.md"],
        "env_required": [],
        "notes": "No credentials. Vera can read but not modify project docs.",
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
    {
        "id": "instagram",
        "label": "Instagram Graph API (community)",
        "description": (
            "Read posts, comments, DMs via Meta Graph API. Long-lived "
            "user access token required (FB Business / Instagram Pro account)."
        ),
        "transport": "stdio",
        "command": ["npx", "-y", "@pinkpixel/instagram-engagement-mcp"],
        "env_required": ["INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID"],
        "notes": (
            "1) Convert your Instagram account to Business/Creator and link to "
            "a Facebook Page. 2) Generate a long-lived user token at "
            "developers.facebook.com/tools/explorer with permissions: "
            "instagram_basic, instagram_manage_comments, instagram_manage_messages, "
            "pages_show_list, pages_read_engagement. 3) Find your IG business "
            "account id via /me/accounts → instagram_business_account.id."
        ),
    },
    {
        "id": "facebook",
        "label": "Facebook Pages (community)",
        "description": (
            "Read/post on Facebook Pages, manage comments and Messenger. "
            "Uses the same Meta Graph token as Instagram."
        ),
        "transport": "stdio",
        "command": ["npx", "-y", "@pinkpixel/facebook-pages-mcp"],
        "env_required": ["FACEBOOK_PAGE_ACCESS_TOKEN", "FACEBOOK_PAGE_ID"],
        "notes": (
            "Page access token (NOT user token). Generate via "
            "/me/accounts and pick the page's access_token. Permissions: "
            "pages_messaging, pages_manage_posts, pages_read_engagement, "
            "pages_read_user_content."
        ),
    },
]


def find(preset_id: str) -> dict | None:
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None
