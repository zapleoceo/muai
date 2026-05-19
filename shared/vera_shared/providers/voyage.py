import httpx

from vera_shared.providers.base import BaseProvider, ProviderError
from vera_shared.tokens.pool import get_pool
from vera_shared.tokens.selector import get_token

_BASE_URL = "https://api.voyageai.com/v1"
_MODEL = "voyage-3"
_CAPABILITY = "embed"


class VoyageProvider(BaseProvider):
    name = "voyage"

    async def chat(
        self, messages: list[dict], capability: str = "chat:fast"
    ) -> tuple[str, int, int]:
        raise NotImplementedError("VoyageProvider does not support chat")

    async def embed(self, text: str) -> list[float]:
        token = await get_token("voyage", _CAPABILITY)
        payload = {"input": [text], "model": _MODEL}
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_BASE_URL}/embeddings", json=payload, headers=headers)

        if resp.status_code != 200:
            await get_pool().on_error(token.id, resp.status_code)
            raise ProviderError(
                f"Voyage error {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )

        data = resp.json()
        return data["data"][0]["embedding"]


_voyage: VoyageProvider | None = None


def get_voyage() -> VoyageProvider:
    global _voyage
    if _voyage is None:
        _voyage = VoyageProvider()
    return _voyage
