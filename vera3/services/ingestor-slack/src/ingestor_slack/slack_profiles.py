"""Кэш профилей Slack-пользователей для поллера."""
from __future__ import annotations

import logging

from ingestor_slack.client import SlackClient

log = logging.getLogger("slack")


class Profiles:
    """Кэш профилей: «id → имя и что известно о человеке».

    Без кэша users.info звался бы на каждое сообщение, а профиль в воркспейсе
    меняется раз в год. Кроме имени берём то, что связывает человека с другими
    каналами: рабочий email — это же алиас gmail, и по нему сущность
    прицепляется к уже существующей, без догадок LLM.
    """

    #: что вытаскиваем из профиля. Не всё подряд: телефон и должность полезны
    #: как контекст, картинки и статус — шум.
    FIELDS = ("email", "phone", "title", "real_name", "display_name")

    def __init__(self, client: SlackClient):
        self._client = client
        self._names: dict[str, str] = {}
        self._profiles: dict[str, dict[str, str]] = {}

    @property
    def known(self) -> dict[str, str]:
        return self._names

    def profile(self, user_id: str) -> dict[str, str]:
        return self._profiles.get(user_id, {})

    async def resolve(self, user_ids: set[str]) -> dict[str, str]:
        for uid in sorted(user_ids - self._names.keys()):
            if not uid:
                continue
            try:
                info = await self._client.user_info(uid)
            except Exception as e:  # noqa: BLE001 — без имени событие всё равно нужно
                log.debug("slack: профиль %s не забрал: %s", uid, e)
                self._names[uid] = uid
                continue
            profile = info.get("profile") or {}
            fields = {}
            for key in self.FIELDS:
                value = str(profile.get(key) or "").strip()
                if value:
                    fields[key] = value
            if info.get("tz"):
                fields["tz"] = str(info["tz"])
            if info.get("is_bot"):
                fields["is_bot"] = "true"
            self._profiles[uid] = fields
            self._names[uid] = str(
                info.get("real_name") or fields.get("real_name")
                or info.get("name") or uid)
        return self._names
