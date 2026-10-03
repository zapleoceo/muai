"""Роль по истории переписки: пакет улик пары → модель → проверенные роли → хранилище.

Роль, извлечённая из одного сообщения, не ловит очевидного: «мой директор» никто не пишет
прямым текстом, а обращение по имени-отчеству, поручения, отчёты и то, как о человеке говорят
третьи лица, видны только в истории. Здесь для устоявшейся пары собирается компактный пакет
(`pair_roles_data` из `event_entities`, выборка по эпохам — `pair_roles_pack`), модель
(capability `structured`, температура 0, через брокер с брейкером сбоев) называет роли из
канонического набора, а `pair_roles_parse` оставляет только подтверждённые дословными цитатами.

Звать модель дорого и незачем, если ничего не изменилось: хэш пакета совпал — только отметка
о проверке. Сбой брокера (в том числе открытый брейкер) прекращает цикл, а не бьёт по очереди; ответ не по
схеме — пауза для этой пары (`save_failure`) и следующая пара.
"""
from __future__ import annotations

import logging
from datetime import datetime

from vera_shared.graph.pair_roles_data import (
    asserted_roles,
    pair_messages,
    pair_sides,
    stats_signals,
    work_context,
)
from vera_shared.graph.pair_roles_pack import build_evidence
from vera_shared.graph.pair_roles_parse import PairRolesFormatError, parse_traced
from vera_shared.graph.pair_roles_prompt import PAIR_ROLES_JSON_SCHEMA, render_prompt
from vera_shared.graph.pair_roles_queue import marker_of, pick_pairs
from vera_shared.graph.pair_roles_signals import text_signals
from vera_shared.graph.pair_roles_store import (
    all_pair_stats,
    all_runs,
    last_run,
    save_failure,
    save_inference,
    touch_run,
)
from vera_shared.graph.pair_roles_types import Evidence, PairInference
from vera_shared.graph.pair_stats import PairStats, ordered, stats_within
from vera_shared.links.context import owner_entity_id
from vera_shared.llm.client import LLMCallFailed, chat_async
from vera_shared.timeutil import utc_naive_now

log = logging.getLogger(__name__)

#: Меньше улик — пара не судится: по трём репликам роль не определить, а модель стоит денег.
MIN_MESSAGES = 6
MAX_OUTPUT_TOKENS = 900
CHARS_PER_TOKEN = 3.5


async def build_pair_evidence(a: int, b: int, stats: PairStats) -> Evidence:
    """Пакет улик пары: сообщения из `event_entities` + структурные сигналы."""
    owner = await owner_entity_id()
    side_a, side_b = await pair_sides(a, b, owner)
    messages, projects = await pair_messages(a, b, owner)
    signals = {"pair_stats": stats_signals(stats), "shared_work_domain_or_slack": await work_context(a, b),
               "projects_of_events": dict(projects.most_common(5)),
               "asserted_in_graph": await asserted_roles(a, b),
               **text_signals(messages)}
    return build_evidence(side_a, side_b, signals, messages)


def owner_label(evidence: Evidence) -> str | None:
    """Метка владельца в пакете (A / B) или None, если пара без владельца."""
    return "A" if evidence.a.is_owner else "B" if evidence.b.is_owner else None


def estimate_tokens(prompt: str) -> int:
    return int(len(prompt) / CHARS_PER_TOKEN)


async def infer_pair(a: int, b: int, *, force: bool = False, dry_run: bool = False,
                     poll_deadline_s: float | None = None) -> PairInference:
    """Вывод ролей одной пары. `dry_run` — всё то же, но ничего не пишется (модель зовётся)."""
    low, high = ordered(a, b)
    stats = (await stats_within([low, high])).get((low, high), PairStats())
    marker = marker_of(stats)
    evidence = await build_pair_evidence(low, high, stats)
    if len(evidence.messages) < MIN_MESSAGES:
        if not dry_run:
            await save_inference(PairInference(low, high, digest=evidence.digest), marker)
        return PairInference(low, high, digest=evidence.digest,
                             skipped=f"мало улик ({len(evidence.messages)} < {MIN_MESSAGES})")
    previous = await last_run(low, high)
    if not force and previous is not None and previous.digest == evidence.digest:
        if not dry_run:
            await touch_run(low, high, marker)
        return PairInference(low, high, digest=evidence.digest, skipped="пакет улик не изменился")
    prompt = render_prompt(evidence)
    try:
        raw, meta = await chat_async(
            messages=[{"role": "user", "content": prompt}], capability="structured",
            response_format=PAIR_ROLES_JSON_SCHEMA, max_tokens=MAX_OUTPUT_TOKENS, temperature=0.0,
            workflow="pair_roles", poll_deadline_s=poll_deadline_s)
        roles, summary, trace = parse_traced(raw, evidence.corpus, evidence.messages, owner_label(evidence))
    except LLMCallFailed as e:
        log.warning("pair_roles %s-%s: LLM не ответила: %s", low, high, e)
        return PairInference(low, high, digest=evidence.digest, failed=True, skipped=str(e)[:200])
    except PairRolesFormatError as e:
        log.warning("pair_roles %s-%s: %s", low, high, e)
        if not dry_run:
            await save_failure(low, high, marker, str(e))
        return PairInference(low, high, digest=evidence.digest, bad_format=True, skipped=str(e))
    cost = float((meta or {}).get("cost_usd") or 0.0)
    model = str((meta or {}).get("model") or (meta or {}).get("provider") or "")[:120]
    log.info("pair_roles %s-%s: ролей=%d сообщений=%d ≈токенов=%d cost_usd=%.6f", low, high,
             len(roles), len(evidence.messages), estimate_tokens(prompt), cost)
    result = PairInference(low, high, tuple(roles), summary, model, evidence.digest, cost,
                           trace=tuple(trace))
    if not dry_run:
        await save_inference(result, marker)
    return result


async def run_cycle(limit: int, now: datetime | None = None) -> list[PairInference]:
    """Один проход фонового цикла: до `limit` самых нужных пар; первый сбой брокера — стоп."""
    owner = await owner_entity_id()
    pairs = pick_pairs(await all_pair_stats(), await all_runs(), owner, limit,
                       now or utc_naive_now())
    out: list[PairInference] = []
    for a, b in pairs:
        result = await infer_pair(a, b)
        out.append(result)
        if result.failed:       # брокер лежит — дальше бить бессмысленно; плохой формат — следующая пара
            break
    return out
