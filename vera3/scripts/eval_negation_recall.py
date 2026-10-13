#!/usr/bin/env python
"""Ручная проверка: отрицательные статусы не попадают в «Что сделано» (не собирается pytest).

    SEARCH_URL=http://localhost:8000 INTERNAL_SECRET=... \
        python vera3/scripts/eval_negation_recall.py --samples 3

Каждая фикстура — выдуманный фрагмент статуса задачи, вшитый в вопрос; запрос идёт в живой
brain-search `/search` с use_agent=false (синтез по одному промпту, без инструментов).
Проверка по структуре ответа: фраза-запрет не должна стоять в разделе «Что сделано»,
обязательные фразы — в «Открыто / ждёт решения» (для контрольного случая — наоборот).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass

import httpx

DONE_HEAD = re.compile(r"^\W*(что сделано|сделано)\b.*$", re.IGNORECASE)
OPEN_HEAD = re.compile(r"^\W*(открыто|ждёт решения|ждет решения)\b.*$", re.IGNORECASE)
FORMAT_HINT = (
    "Ответь двумя разделами с заголовками «Что сделано» и «Открыто / ждёт решения»; "
    "каждый пункт — в подходящий раздел."
)


@dataclass(frozen=True)
class Case:
    name: str
    snippet: str
    forbidden_in_done: tuple[str, ...]
    required: tuple[str, ...]
    expect_done: bool = False


CASES = (
    Case("uk-opis", "SIN-9001: лише опис, без змін у коді. Реалізація не почата.",
         ("SIN-9001",), ("SIN-9001",)),
    Case("ru-no-changes", "SIN-9002: только описание, изменений нет.",
         ("SIN-9002",), ("SIN-9002",)),
    Case("en-no-code", "SIN-9003: description only, no code changes.",
         ("SIN-9003",), ("SIN-9003",)),
    Case("uk-not-implemented", "SIN-9004: не реалізовано, рішення не прийнято.",
         ("SIN-9004",), ("SIN-9004",)),
    Case("ru-options", "SIN-9005: варианты (а)/(б), решение за Дмитрием Корчевским.",
         ("SIN-9005",), ("SIN-9005",)),
    Case("mr-staging", "SIN-9006: MR !1023 on staging, ждёт ревью.",
         ("SIN-9006", "1023"), ("SIN-9006",)),
    Case("side-remark-done", "SIN-9008: Done, MR !1025 merged, изменений в API нет.",
         (), ("SIN-9008",), expect_done=True),
    Case("control-done", "SIN-9007: статус Done, MR !1024 merged, выкачено на прод.",
         (), ("SIN-9007",), expect_done=True),
)


def sections(answer: str) -> tuple[str, str]:
    done: list[str] = []
    other: list[str] = []
    target = other
    for line in answer.splitlines():
        if DONE_HEAD.match(line):
            target = done
        elif OPEN_HEAD.match(line):
            target = other
        else:
            target.append(line)
    return "\n".join(done), "\n".join(other)


def check(case: Case, answer: str) -> str | None:
    done, other = sections(answer)
    if not done and not other.strip():
        return "пустой ответ"
    if case.expect_done:
        missing = [p for p in case.required if p not in done]
        return f"не в «Что сделано»: {missing}" if missing else None
    leaked = [p for p in case.forbidden_in_done if p in done]
    if leaked:
        return f"в «Что сделано»: {leaked}"
    missing = [p for p in case.required if p not in other]
    return f"нет в «Открыто»: {missing}" if missing else None


async def ask(client: httpx.AsyncClient, url: str, secret: str, case: Case) -> str:
    question = f"Что по задаче? Источник: «{case.snippet}». {FORMAT_HINT}"
    r = await client.post(f"{url}/search", json={"q": question, "use_agent": False},
                          headers={"X-Internal-Secret": secret})
    r.raise_for_status()
    return str(r.json()["answer"])


async def main(args: argparse.Namespace) -> int:
    url = os.environ.get("SEARCH_URL", "")
    secret = os.environ.get("INTERNAL_SECRET", "")
    if not url or not secret:
        print("нужны SEARCH_URL и INTERNAL_SECRET в окружении", file=sys.stderr)
        return 2
    failed = 0
    async with httpx.AsyncClient(timeout=120.0) as client:
        print(f"{'case':<20} {'ok/N':<6} first failure")
        for case in CASES:
            errors = [check(case, await ask(client, url, secret, case))
                      for _ in range(args.samples)]
            bad = [e for e in errors if e]
            failed += bool(bad)
            ok = f"{args.samples - len(bad)}/{args.samples}"
            print(f"{case.name:<20} {ok:<6} {bad[0] if bad else '-'}")
    print("PASS" if not failed else f"FAIL: {failed} case(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3)
    sys.exit(asyncio.run(main(parser.parse_args())))
