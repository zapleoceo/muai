"""SQL пересборки `pair_stats` (только Postgres: jsonb, регэксп, FILTER).

Один SELECT, собранный из именованных кусков: каждый отвечает на один вопрос и
читается отдельно. Параметры: `:owner` — telegram id владельца, `:max_authors`
— потолок размера чата, `:work_projects` — проекты, чьи чаты считаются рабочими.
"""
from __future__ import annotations

_EMAIL_RE = "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+"
_GROUP_CHAT_TYPES = "('channel', 'chat', 'supergroup', 'group')"

# Только люди: у организации «личных контактов» нет, а письма-уведомления GitLab
# или банка не должны превращаться в «связь с владельцем».
_PEOPLE = """
owner AS (
    SELECT entity_id AS id FROM entity_aliases
    WHERE source = 'telegram' AND identifier = 'user:' || :owner LIMIT 1),
alias_person AS (
    SELECT a.source, a.identifier, a.entity_id
    FROM entity_aliases a JOIN entities p ON p.id = a.entity_id AND p.type = 'person'
    WHERE a.source IN ('telegram', 'slack', 'gmail'))"""

# (чат, человек, день), когда человек писал в групповом чате. Slack-каналы — это
# рабочее пространство, поэтому ниже они всегда «рабочие».
_CHAT_DAYS = f"""
chat_days AS (
    SELECT 'tg:' || (e.metadata->>'chat_id') AS chat, a.entity_id AS pid,
           e.occurred_at::date AS d
    FROM events e JOIN alias_person a
      ON a.source = 'telegram' AND a.identifier = 'user:' || (e.metadata->>'sender_id')
    WHERE e.source = 'telegram' AND e.metadata->>'chat_type' IN {_GROUP_CHAT_TYPES}
    GROUP BY 1, 2, 3
    UNION
    SELECT 'sl:' || (e.metadata->>'channel_id'), a.entity_id, e.occurred_at::date
    FROM events e JOIN alias_person a
      ON a.source = 'slack' AND a.identifier = 'user:' || (e.metadata->>'sender_id')
    WHERE e.source = 'slack' AND e.metadata->>'channel_kind' <> 'im'
    GROUP BY 1, 2, 3)"""

# Ключ рабочего чата в project_membership — модуль chat_id, без минуса.
_CHATS = """
work_keys AS (
    SELECT key FROM project_membership
    WHERE kind = 'chat' AND project = ANY(CAST(:work_projects AS text[]))),
small_chats AS (
    SELECT chat, (chat LIKE 'sl:%'
                  OR ltrim(substr(chat, 4), '-') IN (SELECT key FROM work_keys)) AS work
    FROM chat_days GROUP BY chat HAVING count(DISTINCT pid) <= :max_authors)"""

# Самосоединение — только по маленьким чатам: сначала отсекаем большие с обеих сторон.
_CO_ACTIVITY = """
small_days AS (
    SELECT d.chat, d.pid, d.d, c.work
    FROM chat_days d JOIN small_chats c ON c.chat = d.chat),
co AS (
    SELECT x.pid AS a, y.pid AS b, x.d, x.chat, x.work
    FROM small_days x
    JOIN small_days y ON y.chat = x.chat AND y.d = x.d AND y.pid > x.pid)"""

# Личка владельца: Telegram (чат с типом user/private, chat_id = id собеседника),
# Slack im (собеседник — единственный не-владелец среди авторов канала), почта
# (только переписка в обе стороны: рассылка и уведомления — не контакт).
_DIRECT = f"""
dm_tg AS (
    SELECT o.id AS a, a.entity_id AS b, e.occurred_at::date AS d
    FROM events e CROSS JOIN owner o
    JOIN alias_person a
      ON a.source = 'telegram' AND a.identifier = 'user:' || (e.metadata->>'chat_id')
    WHERE e.source = 'telegram' AND e.metadata->>'chat_type' IN ('user', 'private')
      AND a.entity_id <> o.id),
im_partner AS (
    SELECT e.metadata->>'channel_id' AS ch, a.entity_id AS pid
    FROM events e CROSS JOIN owner o
    JOIN alias_person a
      ON a.source = 'slack' AND a.identifier = 'user:' || (e.metadata->>'sender_id')
    WHERE e.source = 'slack' AND e.metadata->>'channel_kind' = 'im' AND a.entity_id <> o.id
    GROUP BY 1, 2),
dm_slack AS (
    SELECT o.id AS a, p.pid AS b, e.occurred_at::date AS d
    FROM events e CROSS JOIN owner o
    JOIN im_partner p ON p.ch = e.metadata->>'channel_id'
    WHERE e.source = 'slack' AND e.metadata->>'channel_kind' = 'im'),
mail AS (
    SELECT lower(substring(CASE WHEN e.metadata->>'direction' = 'sent'
                                THEN e.metadata->>'to' ELSE e.metadata->>'from' END
                           from '{_EMAIL_RE}')) AS addr,
           e.metadata->>'direction' AS dir, e.occurred_at::date AS d
    FROM events e
    WHERE e.source = 'gmail' AND e.metadata->>'direction' IN ('sent', 'received')),
mail_two_way AS (
    SELECT addr FROM mail GROUP BY addr
    HAVING bool_or(dir = 'sent') AND bool_or(dir = 'received')),
dm_mail AS (
    SELECT o.id AS a, a.entity_id AS b, m.d
    FROM mail m JOIN mail_two_way t ON t.addr = m.addr CROSS JOIN owner o
    JOIN alias_person a ON a.source = 'gmail' AND a.identifier = m.addr
    WHERE a.entity_id <> o.id)"""

_TOUCH = """
touch AS (
    SELECT least(a, b) AS ea, greatest(a, b) AS eb, d, 'co' AS kind, chat, work FROM co
    UNION ALL SELECT least(a, b), greatest(a, b), d, 'dm', NULL, false FROM dm_tg
    UNION ALL SELECT least(a, b), greatest(a, b), d, 'dm', NULL, false FROM dm_slack
    UNION ALL SELECT least(a, b), greatest(a, b), d, 'mail', NULL, false FROM dm_mail),
stats AS (
    SELECT ea, eb,
           count(*) FILTER (WHERE kind = 'dm') AS dm_msgs,
           count(DISTINCT d) FILTER (WHERE kind = 'dm') AS dm_days,
           count(*) FILTER (WHERE kind = 'mail') AS mail_msgs,
           count(DISTINCT d) FILTER (WHERE kind = 'mail') AS mail_days,
           count(DISTINCT d) FILTER (WHERE kind = 'co') AS co_days,
           count(DISTINCT d) FILTER (WHERE kind = 'co' AND work) AS work_co_days,
           count(DISTINCT chat) FILTER (WHERE kind = 'co') AS co_chats,
           count(DISTINCT d) AS active_days, min(d) AS first_at, max(d) AS last_at
    FROM touch GROUP BY ea, eb)"""

_SHARED_GROUPS = """
small_groups AS (
    SELECT parent_entity_id AS p FROM memberships WHERE is_current
    GROUP BY 1 HAVING count(DISTINCT child_entity_id) <= :max_authors),
shared AS (
    SELECT m1.child_entity_id AS ea, m2.child_entity_id AS eb,
           count(DISTINCT m1.parent_entity_id) AS n
    FROM memberships m1
    JOIN small_groups g ON g.p = m1.parent_entity_id
    JOIN memberships m2 ON m2.parent_entity_id = m1.parent_entity_id
                       AND m2.child_entity_id > m1.child_entity_id
    JOIN entities pa ON pa.id = m1.child_entity_id AND pa.type = 'person'
    JOIN entities pb ON pb.id = m2.child_entity_id AND pb.type = 'person'
    WHERE m1.is_current AND m2.is_current
    GROUP BY 1, 2)"""

PAIR_STATS_COLUMNS = ("entity_a, entity_b, dm_msgs, dm_days, mail_msgs, mail_days, co_days, "
                      "work_co_days, co_chats, shared_groups, active_days, first_at, last_at")

PAIR_STATS_SELECT = "WITH" + ",".join((
    _PEOPLE, _CHAT_DAYS, _CHATS, _CO_ACTIVITY, _DIRECT, _TOUCH, _SHARED_GROUPS)) + """
SELECT coalesce(s.ea, g.ea), coalesce(s.eb, g.eb),
       coalesce(s.dm_msgs, 0), coalesce(s.dm_days, 0),
       coalesce(s.mail_msgs, 0), coalesce(s.mail_days, 0),
       coalesce(s.co_days, 0), coalesce(s.work_co_days, 0), coalesce(s.co_chats, 0),
       coalesce(g.n, 0), coalesce(s.active_days, 0), s.first_at, s.last_at
FROM stats s FULL JOIN shared g ON g.ea = s.ea AND g.eb = s.eb"""
