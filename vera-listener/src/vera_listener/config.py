"""Конфиг слушателя: файл + переменные окружения, без лишних зависимостей.

Секреты берём из ~/.vera/listener.env, а если его нет — из уже существующего
~/.claude/vera_sync.env, который завёл claude_chat_sync. Два файла с одним и
тем же INTERNAL_SECRET разъезжаются, поэтому второй читается как запасной.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
ENV_FILE = HOME / ".vera" / "listener.env"
LEGACY_ENV_FILE = HOME / ".claude" / "vera_sync.env"
# Корень НЕ в AppData\Local: у упакованных (MSIX) приложений он
# виртуализирован — процесс внутри контейнера видит там свои файлы, а задача
# планировщика снаружи их не находит и падает с 0x80070002. Поймано вживую при
# первой установке. ~/.vera рядом с конфигом не редиректится ни у кого.
DEFAULT_ROOT = HOME / ".vera" / "listener"

# Системный звук берём только из того, что похоже на разговор. Ютуб, музыка и
# фильмы в мозг не идут: это шум, который жёг бы распознавание впустую.
DEFAULT_ALLOW_APPS = (
    "zoom.exe", "teams.exe", "ms-teams.exe", "telegram.exe", "discord.exe",
    "slack.exe", "whatsapp.exe", "skype.exe", "webexmta.exe",
)
# Браузеры — условно: Meet и веб-мессенджеры неотличимы от ютуба
# по имени процесса; решает gate.py по наличию речи в микрофоне.
DEFAULT_BROWSER_APPS = (
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "arc.exe",
    "opera.exe", "vivaldi.exe",
)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _split(value: str) -> tuple[str, ...]:
    return tuple(x.strip().lower() for x in value.split(",") if x.strip())


def _split_keep_case(value: str) -> tuple[str, ...]:
    """Как `_split`, но без приведения к нижнему регистру.

    Имена и термины для подсказки распознаванию важны буквами: «LAMAS»,
    приведённое к «lamas», модель напишет строчными и, скорее всего, не
    точнее, чем без подсказки вовсе — Whisper учитывает регистр промпта.
    """
    return tuple(x.strip() for x in value.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    gateway_url: str = "https://dima.veranda.my"
    internal_secret: str = ""
    root: Path = DEFAULT_ROOT
    #: Модель под OpenVINO — уже сконвертированная, качается один раз.
    #: turbo вместо small: на нейропроцессоре она в 2.3 раза быстрее
    #: прежней и жрёт в 19 раз меньше процессора (замеры в transcriber.py).
    model_id: str = "OpenVINO/whisper-large-v3-turbo-int8-ov"
    #: NPU — самое дешёвое по процессору (22% ядра против 189% раньше).
    #: GPU быстрее, но слушатель работает сутками, и Arc нужнее владельцу.
    #: Нет устройства — сам откатится на CPU, см. device_chain().
    stt_device: str = "NPU"
    language: str = "ru"
    silence_timeout_s: float = 60.0
    # Предохранитель по длительности. Встреча длиннее не теряется — она режется
    # на части, но каждая часть осмысляется отдельно, поэтому крутить его руками
    # приходится по-настоящему. Сервер считает свой порог свёртки от этого же
    # значения, независимой константы там больше нет.
    max_session_s: float = 7200.0
    min_speech_s: float = 25.0
    monologue_speech_s: float = 45.0
    chunk_speech_s: float = 60.0
    allow_apps: tuple[str, ...] = field(default=DEFAULT_ALLOW_APPS)
    browser_apps: tuple[str, ...] = field(default=DEFAULT_BROWSER_APPS)
    deny_apps: tuple[str, ...] = ()
    send_interval_s: float = 30.0
    send_backoff_max_s: float = 900.0
    #: Имена и термины, которые модель обычно коверкает: без подсказки
    #: «LAMAS» распознаётся как «LAMRS», «Веранда» — как «Veranda» латиницей
    #: (замер 2026-08-31, см. transcriber.py). Пусто по умолчанию — слушатель
    #: не должен нести чужие имена в исходниках, это личный список владельца.
    glossary: tuple[str, ...] = ()

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def log_file(self) -> Path:
        return self.root / "listener.log"

    @property
    def model_dir(self) -> Path:
        """Модель и кэш компиляции OpenVINO. Рядом с очередью — один корень
        на всё состояние слушателя, чтобы переезд был копированием каталога."""
        return self.root / "models"


def load_config() -> Config:
    """Приоритет: переменные окружения > listener.env > vera_sync.env > дефолт."""
    values = {**_read_env_file(LEGACY_ENV_FILE), **_read_env_file(ENV_FILE)}
    values.update({k: v for k, v in os.environ.items() if k.startswith("VERA_")
                   or k == "INTERNAL_SECRET"})

    def get(key: str, default: str) -> str:
        return values.get(key, default)

    return Config(
        gateway_url=get("VERA_GATEWAY_URL", Config.gateway_url).rstrip("/"),
        internal_secret=get("INTERNAL_SECRET", ""),
        root=Path(get("VERA_LISTENER_ROOT", str(DEFAULT_ROOT))),
        model_id=get("VERA_MODEL_ID", Config.model_id),
        stt_device=get("VERA_STT_DEVICE", Config.stt_device),
        language=get("VERA_LANGUAGE", Config.language),
        silence_timeout_s=float(get("VERA_SILENCE_S", str(Config.silence_timeout_s))),
        max_session_s=float(get("VERA_MAX_SESSION_S", str(Config.max_session_s))),
        min_speech_s=float(get("VERA_MIN_SPEECH_S", str(Config.min_speech_s))),
        monologue_speech_s=float(
            get("VERA_MONOLOGUE_S", str(Config.monologue_speech_s))),
        chunk_speech_s=float(get("VERA_CHUNK_SPEECH_S", str(Config.chunk_speech_s))),
        send_interval_s=float(get("VERA_SEND_INTERVAL_S", str(Config.send_interval_s))),
        send_backoff_max_s=float(
            get("VERA_SEND_BACKOFF_MAX_S", str(Config.send_backoff_max_s))),
        allow_apps=_split(get("VERA_ALLOW_APPS", ",".join(DEFAULT_ALLOW_APPS))),
        browser_apps=_split(get("VERA_BROWSER_APPS", ",".join(DEFAULT_BROWSER_APPS))),
        deny_apps=_split(get("VERA_DENY_APPS", "")),
        glossary=_split_keep_case(get("VERA_GLOSSARY", "")),
    )
