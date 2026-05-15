from __future__ import annotations

import json
import logging

from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Message, Setting
from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider

logger = logging.getLogger(__name__)

_SETTING_KEY = "style_profile"

_STYLE_ANALYSIS_PROMPT = (
    "Ты анализируешь стиль письма человека по его сообщениям в Telegram. "
    "На основе предоставленных сообщений составь краткий профиль стиля (6-12 предложений). "
    "Обрати внимание на: тон (формальный/неформальный/дружеский), длину сообщений, "
    "использование эмодзи, характерные фразы, переключение языков (ru/en/ua), "
    "пунктуацию и заглавные буквы, манеру задавать вопросы или давать ответы. "
    "Пиши результат как инструкцию для AI-ассистента — от второго лица, начиная с: "
    "'Ты отвечаешь в манере своего владельца: ...' "
    "Только профиль, без вступлений и пояснений."
)


async def build_style_profile() -> str:
    async with AsyncSessionLocal() as session:
        rows = list(
            (await session.execute(
                select(Message.text)
                .where(Message.direction == "out", Message.text.isnot(None), Message.text != "")
                .order_by(Message.date_utc.desc())
                .limit(600)
            )).scalars().all()
        )

    if not rows:
        logger.warning("style_profile: no outgoing messages found")
        return ""

    sample_lines = [r for r in rows if r and len(str(r).strip()) >= 3][:500]
    sample = "\n---\n".join(sample_lines)[:14_000]

    provider = get_llm_provider()
    profile = await provider.complete(
        [LLMMessage(role="user", content=sample)],
        system_prompt=_STYLE_ANALYSIS_PROMPT,
    )
    profile = profile.strip()

    async with AsyncSessionLocal() as session:
        existing = (await session.execute(
            select(Setting).where(Setting.key == _SETTING_KEY)
        )).scalar_one_or_none()
        val = json.dumps({"profile": profile}, ensure_ascii=False)
        if existing:
            existing.value = val
        else:
            session.add(Setting(key=_SETTING_KEY, value=val))
        await session.commit()

    logger.info("style_profile: built (%d chars)", len(profile))
    return profile


async def get_style_profile() -> str | None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(Setting).where(Setting.key == _SETTING_KEY)
        )).scalar_one_or_none()
    if not row or not row.value:
        return None
    try:
        return json.loads(row.value).get("profile")
    except Exception:
        return None
