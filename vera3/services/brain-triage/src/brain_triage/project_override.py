"""Детерминированный оверрайд `events.project` по папкам/аккаунтам —
источник истины `project_membership` (см. docs/domain-model.md). Побеждает
LLM-догадку для известных чатов/ящиков и ручных заметок. Вызывается из
process_pending() после каждого triage-батча, в своей отдельной сессии/
транзакции — не должен делить транзакцию с записью triage-статуса или
эмбеддингов."""
from __future__ import annotations

from sqlalchemy import text
from vera_shared.db.engine import get_session

from brain_triage.config import CHAT_CANON


async def apply_project_override(batch_ids: list[int]) -> None:
    if not batch_ids:
        return
    async with get_session() as s:
        await s.execute(text(f"""
            UPDATE events e SET project = pm.project
            FROM project_membership pm
            WHERE e.id = ANY(:ids) AND pm.kind='chat' AND e.source='telegram'
              AND e.metadata->>'chat_id' IS NOT NULL
              AND {CHAT_CANON} = pm.key::bigint
              AND e.project IS DISTINCT FROM pm.project
        """), {"ids": batch_ids})
        # Аккаунт — ключ на любой источник, не только на почту. Значения по
        # источникам не пересекаются (`zaporozec_d@itstep.org` у gmail,
        # `Sintegrum Team/dimondra` у slack, `userbot` у telegram), поэтому
        # шаблон membership бьёт ровно туда, куда заведён.
        #
        # Slack сюда попал по прямому правилу владельца: вся переписка в
        # рабочем пространстве — itstep. Догадка модели там systematically
        # врала на коротких репликах: «)))» и «згоден» уезжали в family и
        # personal, 174 события из 815.
        await s.execute(text("""
            UPDATE events e SET project = pm.project
            FROM project_membership pm
            WHERE e.id = ANY(:ids) AND pm.kind='account'
              AND e.account ILIKE pm.key
              AND e.project IS DISTINCT FROM pm.project
        """), {"ids": batch_ids})
        # itstep/veranda для telegram — ТОЛЬКО из папок/имён. LLM-догадку
        # этих проектов на чате вне membership сбрасываем в 'other'.
        await s.execute(text(f"""
            UPDATE events e SET project = 'other'
            WHERE e.id = ANY(:ids) AND e.source='telegram'
              AND e.project IN ('itstep','veranda')
              AND (e.metadata->>'chat_id') IS NOT NULL
              AND {CHAT_CANON} NOT IN (
                  SELECT key::bigint FROM project_membership WHERE kind='chat')
        """), {"ids": batch_ids})
        # Ручные заметки (source='manual', напр. саммари созвонов, дневные
        # апдейты) несут явный metadata.project_hint от автора — сильнее
        # LLM-догадки по тексту. 'stepan' маппится в 'itstep': Stepan —
        # продукт IT STEP Jakarta, не отдельный проект.
        await s.execute(text("""
            UPDATE events e SET project = 'itstep'
            WHERE e.id = ANY(:ids) AND e.source='manual'
              AND e.metadata->>'project_hint' IN ('itstep', 'stepan')
              AND e.project IS DISTINCT FROM 'itstep'
        """), {"ids": batch_ids})
