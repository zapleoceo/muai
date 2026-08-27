"""Придержанный системный звук: ролик это или созвон — решает микрофон.

Ворота `gate.judge` отделяют разговор от фонового ролика по одному признаку:
у двустороннего разговора звучат обе дорожки, у ютуба — только системная. Но
считались они ТОЛЬКО при закрытии сессии, то есть после того, как ролик уже
распознан целиком. Замер за 2026-08-27: 257 минут аудио прогнано через whisper,
сохранено 5 сессий из 27; среди «разговоров» — доклад с ютуба, окно Claude и
Total Commander. Полтора ядра горели на материал, который выбрасывался.

Теперь системный звук, пока сессия не прошла ворота, копится ЗДЕСЬ, а не идёт
в распознавание. Заговорил микрофон — накопленное уходит распознаваться и
ничего не потеряно; сессию выбросили — накопленное выбрасывается вместе с ней,
не стоив ни одного такта.

Память ограничена: PCM16 16 кГц это 32 КБ/с, и предел задаётся в байтах, а не
в кусках. Упёрлись — забываем самое старое и помним, сколько секунд забыли:
молчаливая потеря хуже честной.
"""
from __future__ import annotations

#: PCM16, 16 кГц, моно — один канал распознавания.
BYTES_PER_S = 16_000 * 2


class Hold:
    """Копилка кусков (смещение, PCM) с потолком по памяти."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._chunks: list[tuple[float, bytes]] = []
        self._bytes = 0
        self._dropped_bytes = 0

    def add(self, offset: float, pcm: bytes) -> None:
        if not pcm:
            return
        self._chunks.append((offset, pcm))
        self._bytes += len(pcm)
        while self._bytes > self.max_bytes and len(self._chunks) > 1:
            _old_offset, old_pcm = self._chunks.pop(0)
            self._bytes -= len(old_pcm)
            self._dropped_bytes += len(old_pcm)

    def take(self) -> list[tuple[float, bytes]]:
        """Забрать всё накопленное. Счётчик забытого не сбрасываем — он про сессию."""
        chunks = self._chunks
        self._chunks = []
        self._bytes = 0
        return chunks

    def clear(self) -> None:
        self._chunks = []
        self._bytes = 0
        self._dropped_bytes = 0

    @property
    def seconds(self) -> float:
        return self._bytes / BYTES_PER_S

    @property
    def dropped_s(self) -> float:
        """Сколько секунд звука забыто из-за потолка памяти."""
        return self._dropped_bytes / BYTES_PER_S

    def __bool__(self) -> bool:
        return bool(self._chunks)
