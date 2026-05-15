import json
import logging
from datetime import datetime

from app.llm.base import LLMMessage
from app.llm.factory import get_router_llm_provider

logger = logging.getLogger(__name__)
from app.services.answering_types import (
    Plan,
    PlanOnEmpty,
    QueryModel,
    QueryOperation,
    QueryOutputShape,
)
from app.services.router_llm.compiler import compile_query_model_to_plan
from app.services.router_llm.fewshots import QUERY_FEWSHOTS
from app.services.router_llm.prompts import router_system_prompt, router_tool_catalog
from app.services.router_llm.router_utils import extract_json
from app.services.router_llm.tg_link import build_plan_for_tg_ref, extract_tg_link_ref


def _time_range_rank(v: str) -> int:
    ranks = {"NONE": 0, "YESTERDAY": 1, "TODAY": 1, "LAST_7_DAYS": 2, "LAST_30_DAYS": 3, "ALL_TIME": 4, "EXPLICIT": 4}
    return int(ranks.get(v, 0))


def _apply_forced_time_range(qm: QueryModel, forced: str) -> QueryModel:
    if _time_range_rank(forced) > _time_range_rank(qm.constraints.time_range.value):
        return qm.model_copy(update={
            "constraints": qm.constraints.model_copy(update={
                "time_range": forced,
                "explicit_from": None,
                "explicit_to": None,
            })
        })
    return qm


async def route_query(
    *,
    query: str,
    user_id: int | None,
    chat_id: int,
    language: str = "ru",
    timezone: str = "UTC",
    state: dict | None = None,
) -> tuple[Plan, str]:
    forced_time_range: str | None = None
    forced_chat_query: str | None = None
    if state:
        f = state.get("force_time_range")
        if isinstance(f, str) and f:
            forced_time_range = f
        fc = state.get("force_chat_query")
        if isinstance(fc, str) and fc:
            forced_chat_query = fc

    tg_ref = extract_tg_link_ref(query)
    if tg_ref:
        plan_dict = build_plan_for_tg_ref(tg_ref)
        plan = Plan.model_validate(plan_dict)
        return plan, json.dumps(plan_dict, ensure_ascii=False)

    provider = get_router_llm_provider()
    now = datetime.now().isoformat(timespec="seconds")

    input_block = {
        "query": query,
        "metadata": {
            "user_id": user_id,
            "chat_id": chat_id,
            "language": language,
            "timezone": timezone,
            "now": now,
        },
        "state": state,
        "catalog": router_tool_catalog(),
        "few_shots": [{"q": q, "query_model": p} for (q, p) in QUERY_FEWSHOTS],
    }

    messages = [LLMMessage(role="user", content=json.dumps(input_block, ensure_ascii=False))]
    system = router_system_prompt()

    raw = await provider.complete(messages, system_prompt=system)
    try:
        qm = QueryModel.model_validate(extract_json(raw))
        if forced_time_range:
            qm = _apply_forced_time_range(qm, forced_time_range)
        if forced_chat_query and not qm.constraints.chat_query:
            qm = qm.model_copy(update={"constraints": qm.constraints.model_copy(update={"chat_query": forced_chat_query})})
        plan = compile_query_model_to_plan(query_model=qm, query=query)
        logger.info(
            "ROUTE op=%s shape=%s scope=%s tr=%s chat_q=%r strategy=%s tools=%s",
            qm.operation.value, qm.output_shape.value,
            qm.constraints.scope.value, qm.constraints.time_range.value,
            qm.constraints.chat_query, plan.strategy.value,
            [t.name for t in plan.tools],
        )
        return plan, raw
    except Exception as exc:
        repair_prompt = (
            "Исправь вывод: верни только валидный JSON объекта QueryModel, без текста. "
            f"Ошибка валидации: {str(exc)[:300]}"
        )
        raw2 = await provider.complete(
            [LLMMessage(role="user", content=raw), LLMMessage(role="user", content=repair_prompt)],
            system_prompt=system,
        )
        try:
            qm2 = QueryModel.model_validate(extract_json(raw2))
            if forced_time_range:
                qm2 = _apply_forced_time_range(qm2, forced_time_range)
            plan2 = compile_query_model_to_plan(query_model=qm2, query=query)
            return plan2, raw2
        except Exception as exc2:
            qm_fallback = QueryModel(
                output_shape=QueryOutputShape.ANSWER,
                operation=QueryOperation.SEARCH,
                need_proof=False,
                clarify_question=(
                    "Не смог корректно разобрать запрос для поиска по базе. "
                    "Уточни, пожалуйста: какой чат/период/что именно нужно найти."
                ),
                max_steps=1,
                on_empty=PlanOnEmpty.ASK_CLARIFY,
                notes=f"router_fallback:{str(exc2)[:120]}",
            )
            plan3 = compile_query_model_to_plan(query_model=qm_fallback, query=query)
            return plan3, json.dumps(qm_fallback.model_dump(), ensure_ascii=False)
