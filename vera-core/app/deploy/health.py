import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


async def health_check(services: list[str], timeout: int = 60) -> dict[str, bool]:
    results: dict[str, bool] = {s: False for s in services}
    deadline = asyncio.get_event_loop().time() + timeout

    async with httpx.AsyncClient(timeout=5.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            pending = [s for s in services if not results[s]]
            if not pending:
                break

            for svc in pending:
                try:
                    resp = await client.get(f"{svc}/health")
                    if resp.status_code == 200:
                        results[svc] = True
                        log.info("Service %s is healthy", svc)
                except Exception:
                    pass

            if any(not v for v in results.values()):
                await asyncio.sleep(3)

    return results
