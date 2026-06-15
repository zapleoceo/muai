# Telegram → Claude connector (remote MCP)

Lets the **Claude app** (iOS/Android/web/desktop) read, search and message
your Telegram account through a custom connector. It exposes the existing
`vera-telegram` Telethon tools over a **remote MCP server** (Streamable HTTP)
protected by **OAuth 2.1 with Dynamic Client Registration** — the auth model
Claude's custom connectors require.

It runs **in-process** inside `vera-telegram` (same Telethon session — a
second process can't open the session file) on a dedicated port, published to
a public subdomain by nginx.

## Tools exposed

Read / search / history (always on):
`telegram_list_recent_dialogs`, `telegram_search_dialogs`,
`telegram_search_public` (global public groups, incl. ones you haven't
joined), `telegram_read_messages`, `telegram_read_messages_batch`,
`telegram_folder_digest`, `telegram_list_folders`, `telegram_get_dialog_info`,
`telegram_list_forum_topics`, plus `telegram_send_message`.

Destructive (`telegram_delete_messages`, `telegram_clear_history`) are
**hidden** unless `MCP_ALLOW_DESTRUCTIVE=true`.

## Configure (`.env`)

```
MCP_ENABLED=true
MCP_PORT=8011
MCP_PUBLIC_URL=https://tg-mcp.veranda.my      # public base, no trailing /
MCP_OAUTH_PASSWORD=<long random>              # owner login gate at /authorize
MCP_OAUTH_SIGNING_SECRET=<long random>        # signs the owner cookie
MCP_OAUTH_DB=/data/mcp_oauth.db
MCP_ALLOW_DESTRUCTIVE=false
```

Generate secrets: `python -c "import secrets;print(secrets.token_urlsafe(48))"`.

## Deploy

1. DNS: point `tg-mcp.veranda.my` at the server (Cloudflare proxied like the
   main site).
2. nginx: `nginx/tg-mcp.conf` proxies the subdomain → `127.0.0.1:8011`. Wire up
   TLS the same way as `dima.veranda.my`.
3. **Allowlist Anthropic's egress IP ranges** at the Cloudflare WAF — Claude
   reaches the endpoint only from those addresses. This is the main network
   control; without it the OAuth password is the only barrier.
4. Redeploy: `docker compose up -d --build vera-telegram`.
5. Health check: `curl https://tg-mcp.veranda.my/.well-known/oauth-authorization-server`.

## Add the connector in Claude

Settings → Connectors → Add custom connector → URL `https://tg-mcp.veranda.my/mcp`.
Claude registers itself (DCR) and opens the OAuth flow; the `/authorize` page
asks for `MCP_OAUTH_PASSWORD`, then Claude completes the handshake. No client
id/secret to copy by hand.

## Security model

- Public endpoint gated by OAuth; `/mcp` rejects any request without a valid
  bearer token (401 + `WWW-Authenticate`).
- The interactive `/authorize` step is gated by the owner password (signed,
  10-min cookie). Only someone who knows the password can mint a token.
- Access tokens are short-lived (1h); refresh tokens rotate on use and are the
  only state persisted (`MCP_OAUTH_DB`).
- DNS-rebinding protection is off because nginx is the host boundary; keep the
  Anthropic IP allowlist in place.
- Destructive tools are off by default.

## What is NOT covered by automated tests

The OAuth + MCP handshake (DCR → authorize → token → `tools/list`) is covered
by `tests/test_mcp_oauth.py` and a protocol-level run. **Live Telethon tool
execution** and **real TLS/DNS** can only be verified after deploy — run a read
tool (e.g. "list my recent Telegram chats") from the Claude app once connected.
