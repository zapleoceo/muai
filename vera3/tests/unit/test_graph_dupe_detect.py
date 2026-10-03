"""Детектор точных дублей: пять случаев аудита и всё, что сливать НЕЛЬЗЯ.

Срез строится руками (`snapshot_from_dict`) — детектор не ходит в БД, поэтому
тесты без SQL. Имена в тестах вымышленные.
"""
from __future__ import annotations

from vera_shared.graph import dupe_detect, dupe_orgs
from vera_shared.graph.dupe_keys import (
    brand_fit,
    is_generic_name,
    name_key,
    org_domain,
    registrable_domain,
)
from vera_shared.graph.dupe_snapshot import snapshot_from_dict


def _snap(*rows):
    """row = (id, type, name, [aliases 'src:ident'], attrs, degree)"""
    entities, aliases, degree = [], [], {}
    for eid, type_, name, als, attrs, deg in rows:
        entities.append({"id": eid, "type": type_, "name": name, "attributes": attrs})
        aliases += [{"entity_id": eid, "source": a.split(":", 1)[0],
                     "identifier": a.split(":", 1)[1]} for a in als]
        degree[str(eid)] = deg
    return snapshot_from_dict({"entities": entities, "aliases": aliases, "degree": degree})


def _tg(eid, name, tg_id, deg=1):
    return (eid, "person", name, [f"telegram:user:{tg_id}"], {"tg_id": tg_id}, deg)


def _work(eid, name, alias, deg=1):
    return (eid, "person", name, [alias], {}, deg)


class TestKeys:

    def test_translit_pairs_collapse(self):
        pairs = [("Виктор Гавриленко", "Viktor Gavrylenko"),
                 ("Вадим Кудрявцев", "Vadim Kudryavtsev"),
                 ("Дмитрий Егоров", "Dmitriy Yegorov"),
                 ("Юлія Гринь", "Yuliia Hryn"),
                 ("Tania Troshchylo", "Таня Трощило")]
        for a, b in pairs:
            assert name_key(a) == name_key(b), (a, b)

    def test_word_order_and_case_do_not_matter(self):
        assert name_key("Sabado Olena") == name_key("olena SABADO")

    def test_single_word_and_emoji_names_have_no_key(self):
        assert name_key("Саша") == ""
        assert name_key("🔥 ✨") == ""

    def test_different_people_keep_different_keys(self):
        assert name_key("Viktor Gavrylenko") != name_key("Viktor Gavrylov")

    def test_registrable_domain(self):
        assert registrable_domain("mail.anthropic.com") == "anthropic.com"
        assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
        assert registrable_domain("x.immigration.gov.vn") == "immigration.gov.vn"
        assert org_domain("me@gmail.com") is None
        assert org_domain("no-reply@mail.anthropic.com") == "anthropic.com"

    def test_generic_names(self):
        for name in ("noreply", "no-reply", "info", "Notification noreply", "a@b.org"):
            assert is_generic_name(name), name
        assert not is_generic_name("Upwork", "upwork@email.upwork.com")

    def test_brand_fit(self):
        assert brand_fit("Kiwi.com", "kiwi.com") == "exact"
        assert brand_fit("Zeno from Resend", "resend.com") == "fits"
        assert brand_fit("noreply", "x.com") == "generic"
        assert brand_fit("Some Person", "github.com") == "foreign"


class TestCase1People:

    def test_telegram_plus_work_person_merge(self):
        snap = _snap(_tg(1, "Виктор Гавриленко", 11, 10),
                     _work(2, "Viktor Gavrylenko", "gmail:vg@itstep.org", 28))
        [action] = dupe_detect.detect_people(snap)
        assert action["action"] == "merge" and action["keep"] == 2 and action["drop"] == [1]

    def test_slack_alias_counts_as_work(self):
        snap = _snap(_tg(1, "Вадим Кудрявцев", 11), _work(2, "Vadim Kudryavtsev", "slack:user:U1"))
        assert dupe_detect.detect_people(snap)[0]["action"] == "merge"

    def test_namesake_telegram_accounts_are_never_merged(self):
        snap = _snap(_tg(1, "Katerina Kravchenko", 11), _tg(2, "Катерина Кравченко", 12),
                     _work(3, "Kateryna Kravchenko", "gmail:kk@itstep.org"))
        [action] = dupe_detect.detect_people(snap)
        assert action["action"] == "skip" and set(action["ids"]) == {1, 2, 3}

    def test_two_telegram_namesakes_without_work_person_do_nothing(self):
        snap = _snap(_tg(1, "Ivan Petrenko", 11), _tg(2, "Іван Петренко", 12))
        assert dupe_detect.detect_people(snap) == []

    def test_work_person_with_outside_email_is_not_a_work_person(self):
        snap = _snap(_tg(1, "Ivan Petrenko", 11), _work(2, "Ivan Petrenko", "gmail:ip@example.com"))
        assert dupe_detect.detect_people(snap) == []

    def test_single_word_names_are_ignored(self):
        snap = _snap(_tg(1, "Саша", 11), _work(2, "Sasha", "gmail:s@itstep.org"))
        assert dupe_detect.detect_people(snap) == []

    def test_heavier_side_is_kept(self):
        snap = _snap(_tg(1, "Ivan Petrenko", 11, 50), _work(2, "Іван Петренко", "gmail:ip@itstep.org", 2))
        assert dupe_detect.detect_people(snap)[0]["keep"] == 1


def _org(eid, name, mail, deg=0, extra=None, type_="organization"):
    aliases = [f"gmail:{mail}"] + (extra or [])
    return (eid, type_, name, aliases, {"email": mail}, deg)


class TestCase2Orgs:

    def test_same_domain_merges_into_domain_entity(self):
        snap = _snap(_org(1, "IT Step", "no-reply@itstep.org"),
                     _org(2, "IT Step", "crm@itstep.org"),
                     (3, "organization", "IT STEP", ["domain:itstep.org"], {}, 88))
        [action] = dupe_orgs.detect_orgs(snap)
        assert action["keep"] == 3 and sorted(action["drop"]) == [1, 2]

    def test_without_domain_entity_exact_brand_name_wins_over_degree(self):
        snap = _snap(_org(1, "Anthropic", "a@mail.anthropic.com", 0),
                     _org(2, "Claude Team Notice", "b@anthropic.com", 9),
                     _org(3, "Anthropic Team", "c@mail.anthropic.com", 3))
        [action] = [a for a in dupe_orgs.detect_orgs(snap) if a["action"] == "merge"]
        assert action["keep"] == 1 and action["drop"] == [3]

    def test_free_mail_domains_are_never_grouped(self):
        snap = _snap(_org(1, "Gmail One", "a@gmail.com"), _org(2, "Gmail Two", "b@gmail.com"))
        assert dupe_orgs.detect_orgs(snap) == []

    def test_shared_platform_domain_is_skipped(self):
        snap = _snap(_org(1, "Alpha Co", "invoice@stripe.com"), _org(2, "Beta Ltd", "x@stripe.com"))
        [action] = dupe_orgs.detect_orgs(snap)
        assert action["action"] == "skip"

    def test_foreign_name_is_left_alone_but_identical_foreign_names_merge(self):
        snap = _snap(_org(1, "Acme", "a@acme.com"), _org(2, "Acme", "b@acme.com"),
                     _org(3, "Jane Roe", "c@acme.com"),
                     _org(4, "Meta Biz", "x@acme.com"), _org(5, "Meta Biz", "y@acme.com"))
        actions = dupe_orgs.detect_orgs(snap)
        merges = [a for a in actions if a["action"] == "merge"]
        skips = [a for a in actions if a["action"] == "skip"]
        assert {frozenset([a["keep"], *a["drop"]]) for a in merges} == {
            frozenset({1, 2}), frozenset({4, 5})}
        assert [s["ids"] for s in skips] == [[3]]

    def test_alias_from_two_domains_is_skipped(self):
        snap = _snap((1, "organization", "Glued", ["gmail:a@one.com", "gmail:b@two.com"], {}, 0))
        assert dupe_orgs.detect_orgs(snap)[0]["action"] == "skip"

    def test_generic_named_org_gets_domain_brand_not_merged_with_strangers(self):
        snap = _snap(_org(1, "noreply", "noreply@tuneprotect.com"),
                     _org(2, "noreply", "noreply@cermatiprotect.com"))
        assert dupe_orgs.detect_orgs(snap) == []
        renames = dupe_orgs.detect_generic_names(snap, set())
        assert {(a["entity"], a["new_name"]) for a in renames} == {
            (1, "Tuneprotect"), (2, "Cermatiprotect")}

    def test_generic_keep_takes_best_name_from_group(self):
        snap = _snap(_org(1, "noreply", "noreply@hetzner.com", 5),
                     _org(2, "Hetzner Online", "billing@hetzner.com"))
        actions = dupe_orgs.detect_orgs(snap)
        assert any(a["action"] == "rename" and a["new_name"] == "Hetzner Online" for a in actions)

    def test_brand_named_org_is_not_renamed(self):
        snap = _snap(_org(1, "Upwork", "upwork@email.upwork.com"))
        assert dupe_orgs.detect_generic_names(snap, set()) == []


def _chat(eid, type_, name, tg_id, deg=1, attrs=None):
    return (eid, type_, name, [f"telegram:chat:{tg_id}"], {"tg_id": tg_id, **(attrs or {})}, deg)


class TestCase3MigratedChats:

    def test_group_plus_supergroup_with_same_name_merge_into_supergroup(self):
        snap = _snap(_chat(1, "group", "Team Chat", 100), _chat(2, "supergroup", "team  chat", 200))
        [action] = dupe_detect.detect_migrated_chats(snap)
        assert action["keep"] == 2 and action["drop"] == [1]

    def test_two_supergroups_with_same_name_are_not_merged(self):
        snap = _snap(_chat(1, "supergroup", "Chat", 100), _chat(2, "supergroup", "Chat", 200))
        assert dupe_detect.detect_migrated_chats(snap) == []

    def test_channel_and_its_discussion_group_are_not_merged(self):
        snap = _snap(_chat(1, "channel", "News", 100), _chat(2, "supergroup", "News", 200))
        assert dupe_detect.detect_migrated_chats(snap) == []

    def test_three_chats_with_one_name_are_skipped_not_guessed(self):
        snap = _snap(_chat(1, "group", "Chat", 100), _chat(2, "supergroup", "Chat", 200),
                     _chat(3, "channel", "Chat", 300))
        [action] = dupe_detect.detect_migrated_chats(snap)
        assert action["action"] == "skip"


class TestCase4Placeholders:

    def test_placeholder_person_merges_into_chat_with_same_tg_id(self):
        snap = _snap((1, "person", "tg_user_555", ["telegram:user:555"],
                      {"tg_id": 555, "username": None}, 1),
                     _chat(2, "channel", "Some Channel", 555))
        [action] = dupe_detect.detect_placeholders(snap)
        assert action["keep"] == 2 and action["drop"] == [1]

    def test_real_person_with_matching_id_is_untouched(self):
        snap = _snap(_tg(1, "Real Person", 555), _chat(2, "channel", "Chan", 555))
        assert dupe_detect.detect_placeholders(snap) == []

    def test_placeholder_without_matching_chat_is_untouched(self):
        snap = _snap((1, "person", "tg_user_555", ["telegram:user:555"], {"tg_id": 555}, 1))
        assert dupe_detect.detect_placeholders(snap) == []

    def test_placeholder_with_username_is_untouched(self):
        snap = _snap((1, "person", "tg_user_555", ["telegram:user:555"],
                      {"tg_id": 555, "username": "someone"}, 1),
                     _chat(2, "channel", "Chan", 555))
        assert dupe_detect.detect_placeholders(snap) == []


class TestCase5ServicePeople:

    def test_service_address_and_brand_name_are_retyped(self):
        snap = _snap(_work(1, "Slack", "gmail:no-reply@slack.com"),
                     _work(2, "Microsoft", "gmail:msa@communication.microsoft.com"),
                     _work(3, "Trip.com", "gmail:trip.com@newsletter.trip.com"))
        retyped = {a["entity"] for a in dupe_detect.detect_service_people(snap)}
        assert retyped == {1, 2, 3}

    def test_human_with_personal_or_work_mail_is_not_retyped(self):
        snap = _snap(_work(1, "Ivan Petrenko", "gmail:ip@itstep.org"),
                     _work(2, "Olga", "gmail:olga@gmail.com"),
                     _tg(3, "Slack Fan", 5))
        assert dupe_detect.detect_service_people(snap) == []

    def test_person_with_telegram_alias_is_never_service(self):
        snap = _snap((1, "person", "Slack", ["gmail:no-reply@slack.com", "telegram:user:5"],
                      {"tg_id": 5}, 0))
        assert dupe_detect.detect_service_people(snap) == []


class TestBuildPlan:

    def test_order_ids_and_no_retype_for_entities_that_get_merged(self):
        snap = _snap(_work(1, "Slack", "gmail:no-reply@slack.com"),
                     _org(2, "Slack", "notification@slack.com", 3),
                     _org(3, "noreply", "noreply@tuneprotect.com"),
                     (4, "person", "tg_user_9", ["telegram:user:9"], {"tg_id": 9}, 1),
                     _chat(5, "group", "Old", 9),
                     _chat(6, "supergroup", "Old", 10))
        plan = dupe_detect.build_plan(snap)
        assert [a["id"] for a in plan] == list(range(1, len(plan) + 1))
        kinds = [(a["case"], a["action"]) for a in plan]
        assert (5, "retype") not in kinds  # Slack-person уйдёт слиянием
        assert kinds.index((2, "rename")) < kinds.index((2, "merge"))
        # заглушка вливается в группу раньше, чем группа — в супергруппу
        assert kinds.index((4, "merge")) < kinds.index((3, "merge"))
