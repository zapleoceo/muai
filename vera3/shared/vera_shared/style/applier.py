"""StyleApplier — turn a StyleProfile + intent into an LLM prompt.

This is the only file that knows how style profiles look in a prompt.
Tools (style.draft) and brain-search both go through here.
"""
from __future__ import annotations

from vera_shared.style.profile import StyleProfile


def render_style_prompt(
    profile: StyleProfile | None,
    listener_label: str,
    intent: str,
    context: str | None = None,
    length_hint: str | None = None,
) -> str:
    """Build the system+user prompt for drafting in Dima's voice."""
    if profile is None or profile.based_on_n_messages == 0:
        return (
            f"Ты пишешь от лица Димы. Адресат: {listener_label}.\n"
            "Профиль стиля для этого собеседника ещё не построен — пиши нейтрально-дружелюбно "
            "на русском, коротко, без излишних формальностей.\n\n"
            f"Намерение: {intent}\n"
            + (f"Контекст: {context}\n" if context else "")
            + (f"Длина: {length_hint}\n" if length_hint else "")
            + "Напиши сообщение. Только текст сообщения, без префиксов и комментариев."
        )

    parts: list[str] = []
    parts.append(f"Ты пишешь от лица Димы. Адресат: {profile.listener_label}.")
    parts.append("Точно сохраняй вот этот голос:")
    parts.append(f"- Обращение: {profile.formality} (vy=на вы, ty=на ты, mixed=смешанное)")
    parts.append(f"- Средняя длина сообщения: ~{profile.avg_length_chars} символов, "
                 f"~{profile.avg_sentences:.1f} предложений")
    if profile.emoji_per_msg > 0.2:
        emo = ", ".join(profile.frequent_emoji[:5])
        parts.append(f"- Эмодзи: ~{profile.emoji_per_msg:.1f} на сообщение, чаще всего: {emo}")
    elif profile.emoji_per_msg < 0.05:
        parts.append("- Эмодзи: не использует")
    if profile.openings:
        parts.append(f"- Типичные начала: {', '.join(profile.openings[:5])}")
    if profile.closings:
        parts.append(f"- Типичные концовки: {', '.join(profile.closings[:5])}")
    if profile.vocabulary_signatures:
        parts.append(f"- Характерные слова/обороты: {', '.join(profile.vocabulary_signatures[:10])}")
    if profile.code_switching:
        langs = ", ".join(f"{k}: {v:.0%}" for k, v in profile.code_switching.items())
        parts.append(f"- Языки в этой переписке: {langs}")

    if profile.sample_messages:
        parts.append("\nПримеры — как Дима ОБЫЧНО пишет именно этому собеседнику:")
        for s in profile.sample_messages[:5]:
            parts.append(f'  «{s.text}»')

    parts.append(f"\nНамерение сейчас: {intent}")
    if context:
        parts.append(f"Контекст: {context}")
    if length_hint:
        parts.append(f"Длина: {length_hint}")
    else:
        parts.append(f"Длина: ~{profile.avg_length_chars} символов ±30%")

    parts.append("\nНапиши сообщение в этом стиле. Только текст, без префиксов и комментариев.")

    return "\n".join(parts)
