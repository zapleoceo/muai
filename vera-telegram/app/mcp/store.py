import aiosqlite

from mcp.server.auth.provider import RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

_CLIENTS = "CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
_REFRESH = ("CREATE TABLE IF NOT EXISTS oauth_refresh_tokens "
            "(token TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL)")


class OAuthStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_CLIENTS)
            await db.execute(_REFRESH)
            await db.commit()

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, data) VALUES (?, ?)",
                (client.client_id, client.model_dump_json()),
            )
            await db.commit()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT data FROM oauth_clients WHERE client_id = ?", (client_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row[0])

    async def save_refresh(self, token: RefreshToken) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO oauth_refresh_tokens (token, client_id, data) "
                "VALUES (?, ?, ?)",
                (token.token, token.client_id, token.model_dump_json()),
            )
            await db.commit()

    async def get_refresh(self, token: str) -> RefreshToken | None:
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT data FROM oauth_refresh_tokens WHERE token = ?", (token,)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return RefreshToken.model_validate_json(row[0])

    async def delete_refresh(self, token: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM oauth_refresh_tokens WHERE token = ?", (token,))
            await db.commit()
