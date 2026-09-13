"""Выгрузка выборки для пилотов моделей (docs/model-pilots.md) — только чтение прода.

Шаг 1: SELECT по events (+ usage_log для фото) через psql в vera3-postgres.
Шаг 2: байты — через /media/download ingestor-telegram изнутри
vera3-media-worker (та же логика, что recognize._download), base64 в stdout.

Всё ложится в --out (по умолчанию D:\\pilots\\data\\<kind>): файлы медиа и
manifest.jsonl с эталонным текстом. Это личные данные владельца —
в git не коммитить, наружу не выкладывать.

    python fetch_samples.py photo --n 38
    python fetch_samples.py voice --n 18
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path

SSH_HOST = "hetzner-root"

# Фото: эталон — ответ gemini-2.5-flash с блоком «Текст:» длиннее 60 символов
# (иначе нечего сравнивать посимвольно). Три корзины, чтобы в выборку попали
# вьетнамские чеки, банковские/денежные скриншоты и прочие документы.
_PHOTO_SQL = r"""
with g as (select distinct event_id from usage_log where workflow='media_vision'
  and success and provider='gemini' and model like 'gemini/gemini-2.5-flash%%'
  and event_id is not null),
c as (
 select e.id, e.metadata->>'chat_id' chat_id, e.metadata->>'msg_id' msg_id,
  case when e.content_text ~ '[ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]' then 'vi'
       when e.content_text ~* 'сбер|тинькофф|банк|перевод|₫|vnd|usd|rp' then 'money'
       else 'other' end grp,
  substring(e.content_text from '--- recognized photo ---(.*)$') ref
 from events e join g on g.event_id=e.id
 where e.metadata->>'media_kind'='photo' and e.content_text like '%%Текст:%%'
   and length(substring(e.content_text from 'Текст:(.*)$'))>60),
r as (select *, row_number() over (partition by grp order by md5(id::text)) rn from c)
select json_build_object('event_id',id,'chat_id',chat_id,'msg_id',msg_id,'group',grp,'ref',ref)
from r where rn <= %(per_group)d order by grp, rn
"""

# Голосовые: эталона-человека нет, опора — текущая расшифровка whisper.
# Украинские выделяем по буквам і/ї/є/ґ, чтобы они точно попали в выборку.
_VOICE_SQL = r"""
with c as (
 select e.id, e.metadata->>'chat_id' chat_id, e.metadata->>'msg_id' msg_id,
  case when t ~ '[іїєґІЇЄҐ]' then 'uk' else 'ru' end grp, t ref
 from (select *, substring(content_text from '--- voice transcription ---\s*(.*)$') t
       from events where source='telegram' and metadata->>'media_kind'='voice') e
 where t is not null and length(t) between 80 and 1500 and t ~ '[а-яА-Я]'),
r as (select *, row_number() over (partition by grp order by md5(id::text)) rn from c)
select json_build_object('event_id',id,'chat_id',chat_id,'msg_id',msg_id,'group',grp,'ref',ref)
from r where rn <= %(per_group)d order by grp, rn
"""

_DOWNLOAD_PY = """
import base64, json, os, httpx
pairs = {pairs}
url = os.environ.get("TELEGRAM_TOOLS_URL", "http://ingestor-telegram:8000")
h = {{"X-Internal-Secret": os.environ["INTERNAL_SECRET"]}}
with httpx.Client(timeout=120) as c:
    for eid, chat, msg in pairs:
        r = c.post(url + "/media/download", json={{"chat_id": int(chat), "msg_id": int(msg)}}, headers=h)
        d = r.json() if r.status_code < 400 else {{"error": "HTTP %s" % r.status_code}}
        print(json.dumps({{"event_id": eid, "b64": d.get("b64"), "mime": d.get("mime"), "error": d.get("error")}}), flush=True)
"""


def _ssh(cmd: str, stdin: str | None = None) -> str:
    res = subprocess.run(["ssh", SSH_HOST, cmd], input=stdin, capture_output=True,
                         text=True, encoding="utf-8", check=True)
    return res.stdout


def select_rows(kind: str, per_group: int) -> list[dict]:
    sql = (_PHOTO_SQL if kind == "photo" else _VOICE_SQL) % {"per_group": per_group}
    out = _ssh("docker exec -i vera3-postgres psql -U vera -d vera -tA", stdin=sql)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def download(rows: list[dict], out_dir: Path, ext: str) -> list[dict]:
    pairs = [(r["event_id"], r["chat_id"], r["msg_id"]) for r in rows]
    out = _ssh("docker exec -i vera3-media-worker python -",
               stdin=_DOWNLOAD_PY.format(pairs=repr(pairs)))
    got = {}
    for line in out.splitlines():
        d = json.loads(line)
        if d.get("b64"):
            path = out_dir / f"{d['event_id']}{ext}"
            path.write_bytes(base64.b64decode(d["b64"]))
            got[d["event_id"]] = path.name
        else:
            print(f"event {d['event_id']}: {d.get('error')}")
    return [dict(r, file=got[r["event_id"]]) for r in rows if r["event_id"] in got]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["photo", "voice"])
    ap.add_argument("--per-group", type=int, default=13)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    out_dir = a.out or Path(r"D:\pilots\data") / a.kind
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = select_rows(a.kind, a.per_group)
    kept = download(rows, out_dir, ".jpg" if a.kind == "photo" else ".ogg")
    with (out_dir / "manifest.jsonl").open("w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{a.kind}: selected {len(rows)}, downloaded {len(kept)} -> {out_dir}")


if __name__ == "__main__":
    main()
