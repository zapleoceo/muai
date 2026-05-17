import httpx

from executor_bot.config import Config


async def register(cfg: Config, bot_username: str, chats: list[dict]) -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{cfg.manager_url}/api/executor/register",
            json={
                "name": cfg.bot_name,
                "bot_username": bot_username,
                "api_url": f"http://executor-bot:{cfg.executor_api_port}",
                "api_secret": cfg.executor_api_secret,
                "chats": chats,
            },
            headers={"Authorization": f"Bearer {cfg.manager_inbox_secret}"},
        )
        r.raise_for_status()
        return r.json()["executor_id"]


async def send_inbox(cfg: Config, executor_id: int, payload: dict) -> None:
    payload["executor_id"] = executor_id
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.post(
            f"{cfg.manager_url}/api/executor/inbox",
            json=payload,
            headers={"Authorization": f"Bearer {cfg.manager_inbox_secret}"},
        )
        r.raise_for_status()
