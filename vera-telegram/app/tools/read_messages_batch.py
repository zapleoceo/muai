"""Read messages from multiple chats in one call — for folder digests."""
import asyncio

from app.tools.read_messages import read_messages


async def read_messages_batch(
    chat_ids: list[int], days: int = 1, limit_per_chat: int = 30,
    ocr_images: bool = False,
) -> dict:
    """Fan-out reads to all chat_ids in parallel, return aggregated dict.
    Skips empty chats from the output. Returns dict:
      {
        "chats_total": N, "chats_with_messages": M,
        "results": [{chat_id, chat_name, messages: [...]}, ...]
      }
    Use BEFORE giving up after a single chat — for «что обсудили
    в папке X» always read ALL chat_ids from list_folders, not the first
    few."""
    async def _one(cid: int) -> dict:
        try:
            return await read_messages(
                peer=str(cid), limit=int(limit_per_chat),
                offset_days=int(days), ocr_images=bool(ocr_images),
            )
        except Exception as exc:
            return {"chat_id": cid, "_error": str(exc)[:120]}

    tasks = [asyncio.create_task(_one(int(c))) for c in (chat_ids or [])]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = []
    with_msgs = 0
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)[:120]})
            continue
        msgs = r.get("messages") or [] if isinstance(r, dict) else []
        if msgs:
            with_msgs += 1
        out.append({
            "chat_id": r.get("chat_id"),
            "chat_name": r.get("chat_name"),
            "messages_count": len(msgs),
            "messages": msgs,
        })

    return {
        "chats_total": len(chat_ids or []),
        "chats_with_messages": with_msgs,
        "days": days,
        "results": out,
    }
