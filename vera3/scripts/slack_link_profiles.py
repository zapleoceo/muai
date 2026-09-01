#!/usr/bin/env python
"""Разовый проход: дописать профили Slack уже заведённым сущностям.

Зачем. `upsert_entity_linked` связывает каналы в момент, когда автор виден
ВПЕРВЫЕ. Люди, заведённые до него, остаются как были: замер 2026-08-26 —
25 slack-сущностей, связанных с другими каналами ноль. Ждать, пока каждый из
них снова напишет, — месяцы.

Что делает: по каждому алиасу `(slack, user:U…)` берёт профиль из `users.info`,
кладёт в атрибуты email / phone / title / tz и, если рабочий email ещё ни за
кем не закреплён, добавляет сущности алиас `(gmail, <email>)` — тогда письмо от
этого человека прилетит на неё, а не создаст ещё одну.

Чего НЕ делает: не сливает уже разъехавшиеся сущности. Если email занят другой
сущностью — значит человек в графе дважды, и это решение владельца на
`/entities/duplicates`, а не ингестора. Такие случаи печатаются отдельно.

    python scripts/slack_link_profiles.py --dry-run   # только показать
    python scripts/slack_link_profiles.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"
                       / "ingestor-slack" / "src"))

from ingestor_slack import auth  # noqa: E402
from ingestor_slack.client import SlackClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from vera_shared.db.engine import get_session, init_engine  # noqa: E402
from vera_shared.db.models_graph import EntityAliasRow, EntityRow  # noqa: E402

log = logging.getLogger("slack-link")

PROFILE_FIELDS = ("email", "phone", "title")


async def slack_people() -> list[tuple[int, str, str]]:
    """→ [(entity_id, slack_user_id, имя)] по всем алиасам slack."""
    async with get_session() as s:
        rows = (await s.execute(
            select(EntityAliasRow.entity_id, EntityAliasRow.identifier, EntityRow.name)
            .join(EntityRow, EntityRow.id == EntityAliasRow.entity_id)
            .where(EntityAliasRow.source == "slack")
            .order_by(EntityRow.name)
        )).all()
    return [(r[0], str(r[1]).removeprefix("user:"), r[2] or "") for r in rows]


async def alias_owner(source: str, identifier: str) -> int | None:
    async with get_session() as s:
        return (await s.execute(
            select(EntityAliasRow.entity_id).where(
                EntityAliasRow.source == source,
                EntityAliasRow.identifier == identifier,
            )
        )).scalar_one_or_none()


async def apply(entity_id: int, fields: dict[str, str], email: str) -> str:
    """→ что сделали: `linked` (добавили алиас gmail) либо `attrs` (только атрибуты)."""
    async with get_session() as s:
        ent = (await s.execute(
            select(EntityRow).where(EntityRow.id == entity_id))).scalar_one()
        # Известное не перетираем: профиль Slack — ещё один свидетель.
        ent.attributes = {**fields, **(ent.attributes or {})}
        if email:
            s.add(EntityAliasRow(entity_id=entity_id, source="gmail",
                                 identifier=email, display_name=ent.name,
                                 confidence=1.0))
    return "linked" if email else "attrs"


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    await init_engine()

    token, _row = await auth.load_token()
    if not token:
        log.error("токена Slack нет — подключи источник в дашборде")
        return
    client = SlackClient(token)

    people = await slack_people()
    log.info("slack-сущностей: %d", len(people))

    linked = attrs_only = split = skipped = 0
    for entity_id, user_id, name in people:
        try:
            info = await client.user_info(user_id)
        except Exception as e:  # noqa: BLE001 — один профиль не должен рвать проход
            log.warning("%s (%s): профиль не забрал: %s", name, user_id, e)
            skipped += 1
            continue

        profile = info.get("profile") or {}
        fields = {k: str(profile[k]).strip() for k in PROFILE_FIELDS
                  if str(profile.get(k) or "").strip()}
        if info.get("tz"):
            fields["tz"] = str(info["tz"])
        fields["slack_user_id"] = user_id

        email = (fields.get("email") or "").lower()
        holder = await alias_owner("gmail", email) if email else None
        if holder is not None and holder != entity_id:
            # Человек в графе дважды. Сливать — решение владельца.
            log.warning("%s: email %s уже у сущности #%s — это дубль, "
                        "решается на /entities/duplicates", name, email, holder)
            split += 1
            email = ""
        elif holder == entity_id:
            email = ""   # алиас уже на месте

        if dry_run:
            log.info("[dry-run] #%s %s: атрибуты %s%s", entity_id, name,
                     sorted(fields), f", алиас gmail {email}" if email else "")
            continue

        what = await apply(entity_id, fields, email)
        if what == "linked":
            linked += 1
            log.info("#%s %s: добавлен алиас gmail %s", entity_id, name, email)
        else:
            attrs_only += 1
        await asyncio.sleep(0.4)   # 50+ req/min у внутреннего приложения

    log.info("итого: алиасов gmail добавлено %d, только атрибуты %d, "
             "дублей найдено %d, профилей не забрано %d",
             linked, attrs_only, split, skipped)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
