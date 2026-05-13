import asyncio
import json
import re

from app.llm.base import LLMMessage
from app.llm.factory import get_llm_provider
from app.services.interactions import list_interactions
from app.services.router_suggestions import create_router_suggestion


_SYSTEM_PROMPT = (
    "Ты анализируешь ошибки роутинга в нашем чат-ассистенте. "
    "На вход ты получаешь кейсы: user_query, router_plan, tool_runs, retrieved_summary, feedback. "
    "Для каждого кейса предложи:\n"
    "- corrected_plan: JSON Plan\n"
    "- rule: короткое правило роутинга на русском\n"
    "Верни только JSON массив объектов вида {interaction_id, query, corrected_plan, rule}."
)


def _extract_json(text: str):
    s = text.strip()
    if s.startswith("[") and s.endswith("]"):
        return json.loads(s)
    m = re.search(r"\[[\s\S]*\]", s)
    if not m:
        raise ValueError("No JSON array found")
    return json.loads(m.group(0))


async def run_once(limit: int = 50) -> int:
    rows = await list_interactions(feedback="dislike", limit=limit, offset=0)
    if not rows:
        return 0

    cases = []
    by_id = {}
    for r in rows:
        iid = int(r.id)
        by_id[iid] = r
        cases.append(
            {
                "interaction_id": iid,
                "query": r.query,
                "router_plan": r.router_plan,
                "tool_runs": r.tool_runs,
                "retrieved_summary": r.retrieved_summary,
                "feedback": {"type": r.feedback, "comment": r.feedback_comment},
            }
        )

    provider = get_llm_provider()
    raw = await provider.complete(
        [LLMMessage(role="user", content=json.dumps({"cases": cases}, ensure_ascii=False))],
        system_prompt=_SYSTEM_PROMPT,
    )
    suggestions = _extract_json(raw)

    created = 0
    for s in suggestions:
        iid = s.get("interaction_id")
        try:
            iid_int = int(iid)
        except Exception:
            continue
        src = by_id.get(iid_int)
        if not src:
            continue
        query = str(s.get("query") or "")
        corrected_plan = s.get("corrected_plan")
        rule = s.get("rule")
        if not query or not corrected_plan:
            continue
        await create_router_suggestion(
            query=query,
            current_plan=src.router_plan,
            proposed_plan=corrected_plan,
            proposed_rule=str(rule) if rule else None,
            context_summary=src.retrieved_summary,
            feedback={"type": src.feedback, "comment": src.feedback_comment},
            meta={"source": "learning_loop", "interaction_id": iid_int},
        )
        created += 1

    return created


def main():
    created = asyncio.run(run_once())
    print(f"Created suggestions: {created}")


if __name__ == "__main__":
    main()
