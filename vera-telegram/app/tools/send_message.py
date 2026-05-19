from app.userbot.client import get_client


async def send_message_to(peer: str, text: str) -> dict:
    client = get_client()
    resolved: str | int = peer
    if peer.lstrip("-").isdigit():
        resolved = int(peer)
    msg = await client.send_message(resolved, text)
    return {"message_id": msg.id, "date": msg.date.isoformat() if msg.date else None}
