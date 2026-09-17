"""Длинный текст: сколько хранить, что отдать модели, как резать на куски.

Замер 2026-09-13 за 30 дней: 163 письма и 95 сессий Claude упёрлись в потолок
8000 символов на входе — хвост терялся навсегда, в том числе для полнотекста.
Одна политика на всех: ингесторы хранят до `MAX_CONTENT_CHARS`, триаж отдаёт
LLM не больше `LLM_EXCERPT_CHARS` (голова + хвост), эмбеддинг длинного текста
идёт кусками (`split_chunks`) в event_chunk_embeddings (миграция 032).
"""
from __future__ import annotations

#: Потолок content_text. 32 тыс., а не «без потолка»: GIN-индекс полнотекста и
#: строка events живут в контейнере на 768m, а письма-рассылки бывают сотнями
#: КБ HTML-мусора. p99 писем на проде упирался ровно в старые 8000, так что
#: реального распределения выше не видно; 32 тыс. — 4× запас при ~4 МБ в месяц
#: прироста (162 письма у потолка × ~24 тыс. символов, TOAST сжимает).
MAX_CONTENT_CHARS = 32_000
#: Потолок events.transcript_text — дословной речи голосового события
#: (миграция 033). Он в 8 раз выше content_text: стенограмму сжимать нельзя,
#: она и есть единственная копия сказанного (звук не хранится). Замер
#: 17.09.2026: 150 голосовых событий за всю историю, самое длинное 151 237
#: символов, всего 1.74 МБ, прирост ~1.5 МБ в месяц. 256 тыс. — запас 1.7×
#: к максимуму и предохранитель от сбойной расшифровки на мегабайты.
MAX_TRANSCRIPT_CHARS = 256_000
#: Сколько текста видит LLM триажа и вектор события — как было до 2026-09-13.
LLM_EXCERPT_CHARS = 8_000
#: Короче — один вектор на событие, как раньше; длиннее — ещё и куски.
#: 4000: на проде 1741 такое событие, ~9.8 тыс. кусков, ~490 событий в месяц.
CHUNK_THRESHOLD = 4_000
CHUNK_CHARS = 1_500
CHUNK_OVERLAP = 200
#: 24 × (1500 − 200) ≈ 31 тыс. — покрывает MAX_CONTENT_CHARS.
MAX_CHUNKS = 24

_EXCERPT_GAP = "\n…\n"
#: Предпочтение мест разреза: абзац, строка, конец предложения, пробел.
_BREAKS = ("\n\n", "\n", ". ", "! ", "? ", "。", " ")


def clip_content(text: str) -> str:
    return text[:MAX_CONTENT_CHARS]


def clip_transcript(text: str) -> str:
    return text[:MAX_TRANSCRIPT_CHARS]


def llm_excerpt(text: str, limit: int = LLM_EXCERPT_CHARS) -> str:
    """Голова и хвост: в письме подпись и последняя реплика ветки — в конце,
    в сессии Claude «Осталось»/«Где» — тоже в конце."""
    if len(text) <= limit:
        return text
    tail = limit // 4
    head = limit - tail - len(_EXCERPT_GAP)
    return text[:head] + _EXCERPT_GAP + text[-tail:]


def needs_chunks(text: str | None) -> bool:
    return len(text or "") > CHUNK_THRESHOLD


def _cut(text: str, start: int, end: int) -> int:
    """Лучшее место разреза в правой половине окна, иначе ровно по окну."""
    floor = start + (end - start) // 2
    for sep in _BREAKS:
        pos = text.rfind(sep, floor, end)
        if pos != -1:
            return pos + len(sep)
    return end


def split_chunks(text: str | None, size: int = CHUNK_CHARS,
                 overlap: int = CHUNK_OVERLAP,
                 max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Куски ≤ size с перекрытием ~overlap, разрез по абзацу/предложению."""
    if overlap >= size:
        raise ValueError("overlap должен быть меньше size")
    body = text or ""
    chunks: list[str] = []
    start = 0
    while start < len(body) and len(chunks) < max_chunks:
        end = min(start + size, len(body))
        cut = end if end == len(body) else _cut(body, start, end)
        piece = body[start:cut].strip()
        if piece:
            chunks.append(piece)
        if cut >= len(body):
            break
        nxt = max(cut - overlap, start + 1)
        # начало следующего куска — с границы слова, не с середины
        space = body.find(" ", nxt, cut)
        start = space + 1 if space != -1 else nxt
    return chunks
