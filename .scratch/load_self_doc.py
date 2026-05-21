import asyncio
from datetime import datetime
from pathlib import Path

from app.graph import write as gw


async def main() -> None:
    vera_md = Path("/var/www/vera/VERA.md")
    self_ext = Path("/var/www/vera/docs/SELF_EXTENSION.md")
    sources = []
    if vera_md.exists():
        sources.append(("vera-self-doc/main", vera_md.read_text(encoding="utf-8")))
    if self_ext.exists():
        sources.append(("vera-self-doc/self-extension",
                        self_ext.read_text(encoding="utf-8")))
    for name, body in sources:
        episode = (
            f"# Документация о Vera (этот эпизод — про меня саму)\n\n{body}\n\n"
            "Я Vera. Когда меня спрашивают про мою архитектуру, возможности, "
            "инструменты или как я устроена — отвечать строго на основе этого "
            "текста, не выдумывать ссылок. Канонический файл доки — VERA.md "
            "в корне репо https://github.com/zapleoceo/muai."
        )
        await gw._add(name, episode, ref_time=datetime.utcnow(),
                      description="self-documentation")
        print(f"loaded {name} ({len(body)} chars)")


asyncio.run(main())
