from vera_shared.tokens.model import TokenRecord
from vera_shared.tokens.pool import get_pool


async def get_token(capability: str) -> TokenRecord:
    return await get_pool().get(capability)
