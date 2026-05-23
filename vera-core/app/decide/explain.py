"""Render a human-readable rationale for a Decision — used by the UX
('Почему?' button) and by Dima's text conversations with Vera."""
from __future__ import annotations

from app.decide.dispatch import Decision


def explain(decision: Decision) -> str:
    """One-paragraph plain Russian: why this action, what alternatives."""
    if decision.chosen is None:
        return "пока не из чего выбирать — не вижу ни одного действия."

    top = decision.chosen
    b = top.breakdown
    bits: list[str] = []

    pc = b.get("pattern_confirm", 0)
    if pc:
        bits.append(f"ты подтверждал такое {pc}× раньше")
    pcorr = b.get("pattern_correct", 0)
    if pcorr:
        bits.append(f"и поправлял {pcorr}×")

    ga = b.get("goal_contribution", 0.5)
    if ga > 0.6:
        bits.append("совпадает с активной целью")

    va = b.get("value_alignment", 0.5)
    if va > 0.6:
        bits.append("соответствует твоим ценностям")

    rev = b.get("reversibility", 0.5)
    if rev >= 0.9:
        bits.append("обратимо одним кликом")
    elif rev <= 0.1:
        bits.append("необратимо — поэтому требуется явное подтверждение")

    why = "; ".join(bits) if bits else "опираюсь на общую структуру графа"
    band_label = {"auto": "автодействие", "propose": "предлагаю",
                  "ask": "спрашиваю"}.get(decision.band, decision.band)
    return (f"«{top.candidate.label}» (alignment={top.score:.1f}, {band_label}). "
            f"Почему: {why}.")
