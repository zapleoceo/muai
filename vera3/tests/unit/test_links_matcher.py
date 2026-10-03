"""Кто упомянут в тексте: полные имена, фамилии, @ники, одиночные имена в круге, прозвища
в области. Данные синтетические, база не нужна."""
from __future__ import annotations

from vera_shared.links.matcher import (
    CONF_FIRST,
    CONF_FULL,
    CONF_SURNAME,
    KIND_NAME,
    KIND_NICKNAME,
    KIND_USERNAME,
    MentionMatcher,
    PersonNames,
)
from vera_shared.links.scope import ChatContext, NicknameRule

OWNER, DIRECTOR, NAMESAKE, LISA, OTHER_DMITRY = 1, 2, 3, 4, 5
PERSONS = [
    PersonNames(OWNER, "Dmitry Testov"),
    PersonNames(DIRECTOR, "Виктор Кронов", ("kronov_v",)),
    PersonNames(NAMESAKE, "Дима"),
    PersonNames(LISA, "Лиза Ветрова"),
    PersonNames(OTHER_DMITRY, "Дмитрий Громов"),
]


def found(text: str, ctx: ChatContext | None = None, matcher: MentionMatcher | None = None,
          author: int | None = None) -> dict[int, tuple[str, float]]:
    m = matcher or MentionMatcher(PERSONS, [])
    return {x.entity_id: (x.kind, x.confidence) for x in m.find(text, ctx or ChatContext(), author)}


def test_full_name_matches_in_any_case_and_alphabet():
    assert found("Виктор Кронов просил ознакомиться")[DIRECTOR] == (KIND_NAME, CONF_FULL)
    assert found("передай Виктору Кронову")[DIRECTOR][1] == CONF_FULL
    assert found("Viktor Kronov wrote")[DIRECTOR][1] == CONF_FULL


def test_distinct_surname_alone_links_but_lowercase_does_not():
    assert found("Кронов сказал")[DIRECTOR] == (KIND_NAME, CONF_SURNAME)
    assert DIRECTOR not in found("кронов сказал")


def test_username_links_with_full_confidence():
    assert found("пишите @Kronov_V")[DIRECTOR] == (KIND_USERNAME, 1.0)


def test_first_name_resolves_only_inside_the_circle():
    inside = ChatContext(participants=frozenset({OWNER, LISA}))
    assert found("Дима, привет", inside)[OWNER] == (KIND_NAME, CONF_FIRST)
    assert found("Дим, привет", inside)[OWNER][0] == KIND_NAME
    # тёзка вне круга не получает чужую реплику, хотя он единственный «Дима» по точному имени
    assert NAMESAKE not in found("Дима, привет", inside)
    assert found("Дима, привет", ChatContext(participants=frozenset({LISA}))) == {}


def test_two_namesakes_in_the_circle_mean_no_link():
    both = ChatContext(participants=frozenset({OWNER, OTHER_DMITRY}))
    assert found("Дима сказал", both) == {}


def test_second_circle_is_used_only_when_the_first_has_nobody():
    ctx = ChatContext(participants=frozenset({LISA}), extended=frozenset({OWNER}))
    assert found("Дмитрий написал", ctx)[OWNER][0] == KIND_NAME
    both = ChatContext(participants=frozenset({OTHER_DMITRY}), extended=frozenset({OWNER}))
    assert found("Дима написал", both)[OTHER_DMITRY][0] == KIND_NAME
    assert OWNER not in found("Дима написал", both)


def test_author_is_not_mentioned_by_his_own_message():
    ctx = ChatContext(participants=frozenset({OWNER, LISA}))
    assert OWNER not in found("Дима на связи", ctx, author=OWNER)


def nickname_matcher(scope: str = "work") -> MentionMatcher:
    return MentionMatcher(PERSONS, [NicknameRule(DIRECTOR, "ВП", True, scope)],
                          {DIRECTOR: frozenset({LISA})})


def test_nickname_counts_in_work_chat_and_not_in_a_public_one():
    work = found("ВП просил ознакомиться", ChatContext(chat_key="telegram:1", is_work=True),
                 nickname_matcher())
    assert work[DIRECTOR] == (KIND_NICKNAME, 1.0)
    m = nickname_matcher().find("ВП нужен в сервисе", ChatContext(chat_key="telegram:2"))
    assert [(x.entity_id, x.scope_ok) for x in m] == [(DIRECTOR, False)]


def test_nickname_is_case_sensitive_and_word_bounded():
    ctx = ChatContext(is_work=True)
    assert nickname_matcher().find("вп сказал", ctx) == []
    assert nickname_matcher().find("ВПР сказал", ctx) == []


def test_nickname_in_dm_with_a_strong_contact_counts():
    dm = ChatContext(chat_key="telegram:9", dm_partner=LISA)
    assert nickname_matcher().find("ВП звонил", dm)[0].scope_ok is True
    stranger = ChatContext(chat_key="telegram:9", dm_partner=999)
    assert nickname_matcher().find("ВП звонил", stranger)[0].scope_ok is False


def test_contacts_scope_needs_two_strong_contacts_in_a_group():
    m = MentionMatcher(PERSONS, [NicknameRule(DIRECTOR, "ВП", True, "contacts")],
                       {DIRECTOR: frozenset({LISA, OTHER_DMITRY})})
    two = ChatContext(chat_key="telegram:3", participants=frozenset({LISA, OTHER_DMITRY}))
    one = ChatContext(chat_key="telegram:3", participants=frozenset({LISA}))
    assert m.find("ВП здесь", two)[0].scope_ok is True
    assert m.find("ВП здесь", one)[0].scope_ok is False


def test_chats_scope_lists_exact_chats_and_global_works_everywhere():
    chats = MentionMatcher(PERSONS, [NicknameRule(DIRECTOR, "ВП", True, "chats", ("telegram:7",))])
    assert chats.find("ВП", ChatContext(chat_key="telegram:7"))[0].scope_ok is True
    assert chats.find("ВП", ChatContext(chat_key="telegram:8"))[0].scope_ok is False
    glob = MentionMatcher(PERSONS, [NicknameRule(DIRECTOR, "ВП", True, "global")])
    assert glob.find("ВП", ChatContext())[0].scope_ok is True


def test_nicknames_can_be_switched_off_for_transcripts():
    assert nickname_matcher().find("ВП", ChatContext(is_work=True), nicknames=False) == []
