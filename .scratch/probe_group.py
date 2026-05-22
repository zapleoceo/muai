import asyncio


async def main() -> None:
    from app.userbot.client import get_client
    c = get_client()
    chat_id = -1003979512448
    try:
        ent = await c.get_entity(chat_id)
        title = getattr(ent, "title", "?")
        forum = bool(getattr(ent, "forum", False))
        megagroup = bool(getattr(ent, "megagroup", False))
        print(f"chat={chat_id}: title={title!r} forum={forum} megagroup={megagroup} ent_id={ent.id}")
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.types import ChannelParticipantsAdmins
        admins = await c(GetParticipantsRequest(
            channel=ent, filter=ChannelParticipantsAdmins(),
            offset=0, limit=50, hash=0,
        ))
        for u in admins.users:
            kind = "bot" if getattr(u, "bot", False) else "user"
            print(f"  admin: {kind} @{u.username or u.first_name} id={u.id}")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")


asyncio.run(main())
