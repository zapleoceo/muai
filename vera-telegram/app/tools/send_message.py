from app.userbot.client import get_client


async def send_message(peer: str, text: str) -> dict:
    client = get_client()

    resolved_peer: str | int = peer
    if peer.lstrip("-").isdigit():
        resolved_peer = int(peer)

    msg = await client.send_message(resolved_peer, text)
    return {
        "message_id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
    }
