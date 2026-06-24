"""Импорт Perplexity .md экспортов в Vera 3.0 brain.

Логика чанкования (та же что я бы применил руками):
- Чистим artefakты Perplexity: <img>, <div align="center">, <span style="display:none">,
  refs ([^N_M]), картинки внизу, "Media generated:..."
- Делим по `^# ` — каждый user-turn становится отдельным событием
  (title = заголовок, body = всё до следующего # или конца файла)
- Если body > 6000 chars — дочинкуем по абзацам, сохраняем
  metadata.chunk_index/total_chunks чтобы можно было восстановить порядок
- source_event_id = "ppx:" + sha1(file_name + heading + chunk)[:16] для дедупа
- occurred_at = mtime файла (как proxy на дату разговора)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx


GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
IMPORT_DIR = os.environ.get("IMPORT_DIR", "/imports/perplexity")
ACCOUNT = "dima_perplexity"

CHUNK_CHAR_LIMIT = 6000

# ─── Чистка ──────────────────────────────────────────────────────────────────

NOISE_PATTERNS = [
    # Логотип Perplexity
    re.compile(r'<img[^>]*pplx[^>]*?>', re.IGNORECASE),
    re.compile(r'<img[^>]*?>', re.IGNORECASE),
    # Hidden span — там обычно список refs
    re.compile(r'<span\s+style="display:none">[\s\S]*?</span>', re.IGNORECASE),
    # Центральный divider
    re.compile(r'<div\s+align="center">[\s\S]*?</div>', re.IGNORECASE),
    # ⁂ как маркер конца секции цитат
    re.compile(r'⁂'),
    # Цифровые цитаты [^1_2]
    re.compile(r'\[\^[0-9_]+\]'),
    # "Media generated: ..."
    re.compile(r'Media generated:.*$', re.MULTILINE),
]

REF_BLOCK_RE = re.compile(
    r'^\[\^[0-9_]+\]:.*?(?=^\[\^[0-9_]+\]:|^# |\Z)',
    re.MULTILINE | re.DOTALL,
)


def clean(text: str) -> str:
    """Убрать perplexity-шум, оставить содержательный текст."""
    for p in NOISE_PATTERNS:
        text = p.sub('', text)
    text = REF_BLOCK_RE.sub('', text)
    # Сжать множественные переводы
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Trim
    text = '\n'.join(line.rstrip() for line in text.split('\n'))
    return text.strip()


# ─── Разбивка ────────────────────────────────────────────────────────────────


def split_into_turns(text: str) -> list[tuple[str, str]]:
    """Делит текст по `^# ` заголовкам.

    Возвращает [(heading, body), ...]. Первый блок до первого # игнорируется.
    """
    # findall не подходит — нужно ловить body до следующего #
    matches = list(re.finditer(r'^# (.+?)$', text, re.MULTILINE))
    if not matches:
        # Нет заголовков — весь файл как один turn
        return [("(no heading)", text.strip())]

    turns = []
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            turns.append((heading, body))
    return turns


def chunk_body(heading: str, body: str) -> list[str]:
    """Если body короче CHUNK_CHAR_LIMIT — один чанк. Иначе по абзацам."""
    full = f"# {heading}\n\n{body}"
    if len(full) <= CHUNK_CHAR_LIMIT:
        return [full]

    paragraphs = [p.strip() for p in body.split('\n\n') if p.strip()]
    chunks: list[str] = []
    current = f"# {heading}\n\n"
    for p in paragraphs:
        if len(current) + len(p) + 4 > CHUNK_CHAR_LIMIT and current.strip() != f"# {heading}":
            chunks.append(current.rstrip())
            current = f"# {heading} (cont.)\n\n{p}\n\n"
        else:
            current += p + "\n\n"
    if current.strip() and current.strip() != f"# {heading}":
        chunks.append(current.rstrip())
    return chunks or [full[:CHUNK_CHAR_LIMIT]]


# ─── POST ────────────────────────────────────────────────────────────────────


async def post_event(client: httpx.AsyncClient, payload: dict) -> bool:
    try:
        r = await client.post(
            f"{GATEWAY_URL}/event/perplexity",
            json=payload,
            headers={"X-Internal-Secret": INTERNAL_SECRET},
        )
        return r.status_code < 400
    except Exception as e:
        print(f"  POST failed: {e}", flush=True)
        return False


async def main():
    base = Path(IMPORT_DIR)
    if not base.exists():
        print(f"✗ {base} not found")
        sys.exit(1)

    files = sorted(base.glob("*.md"))
    if not files:
        print(f"✗ no .md in {base}")
        sys.exit(1)

    print(f"Found {len(files)} files in {base}", flush=True)

    total_events = 0
    total_files = 0
    failures = 0

    async with httpx.AsyncClient(timeout=20) as client:
        for path in files:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"✗ {path.name}: read failed: {e}")
                continue

            cleaned = clean(raw)
            turns = split_into_turns(cleaned)
            occurred = datetime.utcfromtimestamp(path.stat().st_mtime)

            chunks_in_file = 0
            for turn_idx, (heading, body) in enumerate(turns):
                chunks = chunk_body(heading, body)
                for chunk_idx, chunk in enumerate(chunks):
                    sig = f"{path.name}|{heading}|{chunk_idx}|{chunk[:200]}"
                    eid = "ppx:" + hashlib.sha1(sig.encode()).hexdigest()[:16]

                    payload = {
                        "source": "perplexity",
                        "source_event_id": eid,
                        "account": ACCOUNT,
                        "category": "research",
                        "content_text": chunk[:8000],
                        "occurred_at": occurred.isoformat(),
                        "metadata": {
                            "file": path.name,
                            "heading": heading[:200],
                            "turn_index": turn_idx,
                            "chunk_index": chunk_idx,
                            "chunks_in_turn": len(chunks),
                            "turns_in_file": len(turns),
                        },
                    }
                    ok = await post_event(client, payload)
                    if ok:
                        chunks_in_file += 1
                        total_events += 1
                    else:
                        failures += 1
                    # лёгкий throttle чтобы gateway/triage не захлебнулись
                    await asyncio.sleep(0.05)

            total_files += 1
            print(f"  ✓ {path.name}: {len(turns)} turns → {chunks_in_file} chunks "
                  f"(file size {len(raw):,} chars)", flush=True)

    print(f"\nDone. {total_files} files, {total_events} events posted, "
          f"{failures} failures.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
