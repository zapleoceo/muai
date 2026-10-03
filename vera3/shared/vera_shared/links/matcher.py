"""Кто упомянут в тексте события — чистые функции, без базы.

Связываем осторожно: лучше пропустить упоминание, чем приписать человеку чужую
реплику. Уверенность по виду улики:

- `username` — «@ник» целиком, 1.0;
- `name` 0.95 — имя целиком: все слова имени рядом (имя + фамилия, имя + отчество);
- `name` 0.9 — имя и отчество («Дмитрий Александрович») у трёхсловного имени;
- `name` 0.8 — характерная фамилия одна (в графе её носит один человек, ≥5 букв,
  написана с заглавной);
- `name` 0.6 — одиночное имя, только среди участников ЭТОГО чата и только если оно
  у одного из них (иначе «Андрей» — любой Андрей);
- `nickname` — прозвище из `entity_nicknames`, регистрозависимое; вне своей области
  возвращается с `scope_ok=False` (нужно счёту и аудиту, потребители его не берут).

Слова сравниваются по `dupe_keys.word_key` (транслит + падежи: «Корчевского» находит
«Korchevsky»). Автор события сам себя не упоминает.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from vera_shared.graph.dupe_keys import name_key
from vera_shared.links.matcher_text import (
    USERNAME,
    WORD,
    find_word,
    name_keys,
    near,
    sentence_initial,
    text_words,
)
from vera_shared.links.name_forms import name_group
from vera_shared.links.scope import (
    ChatContext,
    NicknameRule,
    in_scope,
    token_pattern,
)

KIND_NAME, KIND_NICKNAME, KIND_USERNAME = "name", "nickname", "username"
CONF_FULL, CONF_PATRONYMIC, CONF_SURNAME, CONF_FIRST = 0.95, 0.9, 0.8, 0.6
MIN_SURNAME_CHARS = 5
MIN_FIRST_CHARS = 3
_STEM = 4


@dataclass(frozen=True)
class PersonNames:
    entity_id: int
    name: str
    usernames: tuple[str, ...] = ()


@dataclass(frozen=True)
class Mention:
    entity_id: int
    token: str
    kind: str
    confidence: float
    scope_ok: bool = True


class MentionMatcher:
    """Индексы по людям и прозвищам; `find` — упоминания в одном тексте."""

    def __init__(self, persons: list[PersonNames], nicknames: list[NicknameRule],
                 strong_contacts: dict[int, frozenset[int]] | None = None) -> None:
        self._persons = {p.entity_id: p for p in persons}
        self._name_words = {p.entity_id: name_keys(p.name) for p in persons}
        # Полное имя, общее у двоих, никого не называет (как `NameResolver.full`).
        counts: dict[str, int] = {}
        for p in persons:
            if key := name_key(p.name):
                counts[key] = counts.get(key, 0) + 1
        ambiguous = {p.entity_id for p in persons if counts.get(name_key(p.name), 0) > 1}
        self._rules = [(rule, token_pattern(rule)) for rule in nicknames]
        self._strong = strong_contacts or {}
        self._usernames = {u.lower(): p.entity_id for p in persons for u in p.usernames}
        surnames: dict[str, list[int]] = {}
        for eid, words in self._name_words.items():
            if eid not in ambiguous and len(words) >= 2 and len(words[-1]) >= MIN_SURNAME_CHARS:
                surnames.setdefault(words[-1], []).append(eid)
        self._surnames = {k: v[0] for k, v in surnames.items() if len(v) == 1}
        self._by_stem: dict[str, list[int]] = {}
        for eid, words in self._name_words.items():
            if eid not in ambiguous and len(words) >= 2:
                for key in {w[:_STEM] for w in words}:
                    self._by_stem.setdefault(key, []).append(eid)

    def find(self, text: str, ctx: ChatContext, author_id: int | None = None,
             scopes: frozenset[str] | None = None) -> list[Mention]:
        """`scopes` — какие виды областей прозвищ искать (None — все; пусто — прозвища не ищутся:
        в расшифровках речи «да» заглавными — слово, а фраза «Имя Отчество» с областью global — человек)."""
        found: dict[tuple[int, str], Mention] = {}
        clean = USERNAME.sub(" ", text)           # ник — не слова имени
        words = text_words(clean)
        for mention in (*self._usernames_in(text), *self._names_in(words, clean),
                        *self._first_names_in(words, ctx),
                        *self._nicknames_in(text, ctx, scopes)):
            if mention.entity_id == author_id:
                continue
            key = (mention.entity_id, mention.token)
            if key not in found or found[key].confidence < mention.confidence:
                found[key] = mention
        return list(found.values())

    def _usernames_in(self, text: str) -> list[Mention]:
        return [Mention(eid, "@" + u, KIND_USERNAME, 1.0)
                for u in USERNAME.findall(text) if (eid := self._usernames.get(u.lower()))]

    def _names_in(self, words: list[tuple[str, str]], text: str) -> list[Mention]:
        candidates = {eid for key, _ in words for eid in self._by_stem.get(key[:_STEM], ())}
        out: list[Mention] = []
        for eid in candidates:
            if mention := self._full_name(eid, words):
                out.append(mention)
        named = {m.entity_id for m in out}
        offsets = [m.start() for m in WORD.finditer(text)]
        lowercase = {key for key, raw in words if raw[0].islower()}
        for (key, raw), offset in zip(words, offsets, strict=True):
            eid = self._surnames.get(key)
            if eid is None or eid in named or not raw[0].isupper():
                continue
            # Фамилия одна, без имени: не в начале предложения («Мельник пришёл» — мельник?) и
            # то же слово не встречается в тексте строчным (тогда это нарицательное).
            if sentence_initial(text, offset) or key in lowercase:
                continue
            out.append(Mention(eid, raw, KIND_NAME, CONF_SURNAME))
        return out

    def _full_name(self, eid: int, words: list[tuple[str, str]]) -> Mention | None:
        name_words = self._name_words[eid]
        positions = [find_word(words, w) for w in name_words]
        last = len(name_words) - 1
        best = 0.0
        for i, j in combinations(range(len(name_words)), 2):
            if positions[i] and positions[j] and near([positions[i], positions[j]]):
                with_surname = last in (i, j)
                if with_surname or len(name_words) >= 3:
                    best = max(best, CONF_FULL if with_surname else CONF_PATRONYMIC)
        return Mention(eid, self._persons[eid].name, KIND_NAME, best) if best else None

    def _first_names_in(self, words: list[tuple[str, str]], ctx: ChatContext) -> list[Mention]:
        """Одиночное имя — только внутри круга разговора и только если оно у одного человека
        круга (в любой форме: Дима = Дмитрий). Два Димы в кругу — связи нет. Второй круг
        (контакты автора) берётся, лишь когда в первом никого подходящего нет."""
        circles = [self._by_first(ctx.participants), self._by_first(ctx.extended)]
        out = []
        for key, raw in words:
            if len(raw) < MIN_FIRST_CHARS or not raw[0].isupper():
                continue
            for by_key in circles:
                owners = by_key.get(key, set()) | by_key.get(("g", name_group(key)), set())
                if owners:
                    if len(owners) == 1:
                        out.append(Mention(next(iter(owners)), raw, KIND_NAME, CONF_FIRST))
                    break
        return out

    def _by_first(self, members: frozenset[int]) -> dict[object, set[int]]:
        by_key: dict[object, set[int]] = {}
        for eid in members:
            for key in self._first_keys(eid):
                by_key.setdefault(key, set()).add(eid)
        return by_key

    def _first_keys(self, eid: int) -> list[object]:
        parts = self._name_words.get(eid) or []
        keys: list[object] = [parts[0]] if parts else []
        keys += [("g", g) for w in parts if (g := name_group(w)) is not None]
        return keys

    def _nicknames_in(self, text: str, ctx: ChatContext,
                      scopes: frozenset[str] | None) -> list[Mention]:
        out = []
        for rule, pattern in self._rules:
            if (scopes is None or rule.scope_kind in scopes) and pattern.search(text):
                ok = in_scope(rule, ctx, self._strong.get(rule.entity_id, frozenset()))
                out.append(Mention(rule.entity_id, rule.token, KIND_NICKNAME, 1.0, ok))
        return out
