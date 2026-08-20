---
name: itstep-report
description: Compose the daily "what I did today" narrative report for the IT STEP Jakarta project (from Telegram/Gmail via the vera3 Postgres DB, plus stepan2 git history), written for the branch director. Use when Dima asks for an ITSTEP daily report, a "today" report, or "отчет о проделанном за сегодня".
---

# ITSTEP daily report

## Length — laconic, not a protocol (set 2026-08-04)

The report is a director's summary, not a transcript. Target **~1800-2200
characters, one Telegram message**. The 2026-08-04 draft ran 7800 characters
across three messages and was rejected as "протокол".

- One block per topic, 2-4 sentences. Blocks in CAPS, no markdown.
- Lead with ГЛАВНОЕ: the single fact that explains the day.
- Keep: decisions, numbers, who blocked what, what changed.
- Cut: the play-by-play of who wrote what at which minute, restatements of
  the same fact from Telegram and email, courtesy exchanges, screenshots
  described, and any sentence that would not change tomorrow's actions.
- Verbatim quotes only when the wording itself is the news ("пока нет денег").
- Stepan section: 3-4 sentences on what changed and why it matters. Never a
  commit list.
- Full detail still gets gathered and audited — it just does not all get
  printed. Depth of research, brevity of output.

Standing format (established, always apply):

- First person throughout ("я сделал", never "ты"/"команда сделала X" as the
  subject of the report — the report is about Dima's own day).
- Narrative prose. No raw event-log tables, no timestamp dumps.
- No em-dashes. No curly/guillemet quotes, plain straight quotes only.
- Call the sales bot "Степан", never "Стёпа".
- Stepan/dev section stays short, at the end, clearly separated from the
  operational narrative above it.
- Never invent a person's full name from a bare handle — if the data only has
  a Telegram @handle, use the handle.
- When the director describes a structure to use (e.g. "signal, decision,
  argument"), apply that structure in the prose, don't echo the label words
  themselves ("Сигнал:", "Решение:") as literal headers — those are examples
  of shape, not text to print.

## Editorial checklist (soft — apply where it fits, don't force it)

The director gave this feedback on 2026-07-15 and 2026-07-16 after reading
reports. Read back over the draft against these before sending. Not every
point applies to every report; skip what doesn't fit rather than padding to
hit every point.

1. **Cover the whole day, not disconnected episodes.** Bridge the gaps
   between named threads so the report reads as one continuous day, not a
   list of unrelated incidents.
2. **Say what Dima personally checked, analyzed, or decided**, not just what
   happened or what the team did. "Ситра прислала отчёт" is a fact; "я
   посмотрел отчёт и увидел X, поэтому решил Y" is the report.
3. **For any ongoing saga** (a running "history with X"), state today's
   concrete actions and the explicit outcome, not just that it "closed" or
   "continued." What did Dima specifically do today, and what was the result
   in hand by end of day.
4. **For checklist-style syncs** (e.g. a numbered list of open items), give
   the decision/status for each point individually, plus the final status of
   whatever deliverable they resolve into (a vacancy post, a case, etc.).
   Don't just say "discussed four points."
5. **Avoid vague summary verbs** ("emphasized," "communicated," "aligned").
   Say the concrete mechanism: what format, what decision, what changed as a
   result.
6. **Frame forward-looking items as Dima's own commitment**, first person
   future tense ("завтра назначу ей онбординг"), not passive necessity
   ("нужно назначить онбординг").
7. **Distinguish "я сделал X" from "команда сделала Y."** When reporting a
   pause/decision (e.g. pausing an ad experiment), state the decision and the
   next step taken, not just the pause itself.
8. **Replace vague time descriptors** ("весь день кусками") with a rough
   estimate of time/effort and a note of what ran in parallel, when that's
   knowable from the data.
9. **Connect raw numbers to action.** A stats dump (leads, conversions, KPI
   snapshot) needs a sentence on what Dima did with it: what he checked,
   concluded, or decided based on those numbers.
10. **In the Stepan section, attribute changes to who actually did them**
    (Dima personally vs. the team/dev) when that's known from context, rather
    than defaulting to passive voice for every fix. Check first — if the git
    commits are all Dima's own, say "я" throughout, not "разработка"/"team".
11. **State Dima's own plan for tomorrow explicitly** — what he personally
    will do next, not a vague forward-looking summary.
12. **Quantify routine setup time** (e.g. the morning task-delegation
    message) when knowable — how long it took, and what filled the rest of
    the day around it.
13. **For any specific problem/incident, give the explicit final outcome**
    (did the candidate actually get in? did the payment go through?), and
    note whether it consumed a significant part of the day — not just the
    actions taken toward it.
14. **For lead/sales numbers, say what Dima personally did with them** —
    called leads, configured something, ran an analysis — and roughly how
    long it took, not just the raw figures.
15. **Give the Stepan section a rough timeline/effort estimate within the
    day**, not just a list of results — how it fit into the day alongside
    everything else, when that's inferable from commit timestamps.
16. **Track follow-through on commitments from the previous report's plan**
    explicitly (e.g. "assign onboarding tomorrow" from yesterday) — state
    whether it happened today, and if not, why.
17. **Carry forward any previously-mentioned upcoming commitment** (an event,
    a deadline) even on a day with no new action on it — give its status or
    the explicit reason for no update, don't let it silently drop out of the
    narrative.
18. **When delegating a task to someone, describe how/when Dima will verify
    or accept the result** (e.g. checking Sergey's ad-copy change), not just
    that the task was handed off.

## Hard accuracy rules (learned 2026-07-21 after a report full of mismatches)

- **Never infer an outcome from Dima's own short reply.** "ok" / "come to me
  after lunch" is NOT "согласовал запуск" — it defers the decision. Only
  claim a decision if the message states one.
- **Before writing "жду ответа" on any thread, check the rest of the day** —
  the counterparty may have replied minutes later. Follow every open thread
  to its actual end-of-day state.
- **Attribute proposals to who actually made them.** If Zee proposed the
  meeting time and asked to include Lisa, don't write "я назначил встречу с
  Лизой". Check the `author_role` of the proposing message.
- **Count things from the source, don't remember them.** Three creatives is
  not two. Re-read the message that enumerates items before writing a number.
- **Completed vs. requested**: "I asked X for help installing" is not
  "installed". Report the actual state, not the expected trajectory.
- **Don't add participants to a conversation who weren't in it** (e.g.
  "с Мальцевым и Сергеем" when only Мальцев spoke that day on that topic).
- These errors came from composing off 450-char truncated excerpts and
  partial mid-day reads. When a thread matters for the report, re-query it
  with a longer `left(content_text, ...)` window (600+) and read the whole
  exchange before writing its outcome.

## Report window — rolling cutoff, NOT midnight

A report covers everything since the PREVIOUS report was delivered, not the
calendar day. Reports get written in the evening (~18:00 WIB), so filtering
on `date_trunc('day', ...)` silently drops the evening, the night, and the
early morning — replies that land after the report, and Stepan commits made
at night, never appear in any report at all.

1. **Read `last-cutoff.json`** in this skill's directory. Use `cutoff_utc`
   as the lower bound for every source:
   ```sql
   WHERE occurred_at > TIMESTAMP '<cutoff_utc>'
   ```
   and `gh api ...commits?since=<cutoff_utc>` for Stepan.
   `occurred_at` is UTC; add `interval '7 hours'` only for display.
2. If the file is missing or its date is more than ~3 days old, fall back to
   midnight WIB of the report day and say so explicitly in the report.
3. Name the window in the report's first line, e.g. "с 18:43 3 августа по
   18:20 4 августа", so it is obvious what was covered.
4. **After delivering the report, rewrite `last-cutoff.json`** with the new
   `now()` from the DB (query it, never guess) plus `max(id)` from `events`.
   Do this every time, or the next report will double-count or skip.

## Data sources and gotchas

- vera3 Postgres on `hetzner-root`: `docker exec vera3-postgres psql -U vera
  -d vera`. Telegram/Gmail events live in `events`, filter
  `source != 'vera_memory'` for real activity and `source = 'vera_memory'`
  for previously-saved atomic facts (e.g. team-meeting notes).
- **ALWAYS query Gmail too (`source = 'gmail'`), every report, no exceptions**
  (missed on 2026-07-22 → "отвратительный отчет"). Email carries whole
  threads that never appear in Telegram: tax/payment approvals (Галина,
  Мардар, Корень), petty-cash approvals (Марина), HQ threads (Катерина,
  Егоров), Дарья's written meeting summaries, Read AI meeting summaries
  (subject "Read Meeting Report" — gives topics/time of calls that
  otherwise leave no trace), Sintegrum reminders, Meta ad approvals. Skim
  all of it; skip pure marketing/newsletter noise.
- **Don't over-trust the known-chats list — it's a floor, not a ceiling.**
  New chats appear (JAKARTA <> MARKETING HQ TEAM, JAKARTA - PAYMENT IT STEP,
  Studing Jakarta internal, ITStep Asia 3, Zee, Excel, Сергей @iTStep_Asian,
  Sergey @Serhii_Shabaldas, Александр/Мальцев, Юлия/Мардар). After the main
  fetch, list ALL of the day's chats and check every unfamiliar title for
  itstep relevance (1:1 chats with HQ/team people count) before dismissing
  it. Known non-itstep: Veranda-set (Старшие и отчеты, Веранда*, Viktoria,
  Ли), Быть Или, news channels, Ilya (personal).
- **Author truth is `metadata->>'author_role'` (`self`|`counterparty`).**
  In a 1:1 chat, `chat_title` is the COUNTERPARTY's name, never the speaker —
  a `self` line in chat "Marina" is Dima speaking TO Marina, not Marina.
  Reversed authorship has recurred repeatedly (HeyGen/Zee, Дарья, Marina) —
  it happens when composing prose off rows where the mind collapses
  "message in Marina's chat" → "Marina said".
## Authorship: mechanical, not disciplinary (rebuilt 2026-08-11)

Five reports in a row reversed who said what, even after the "print the
direction" and "write an audit table" rules existed. A rule that can be
skipped under time pressure gets skipped. So the check is now code.

**1. Fetch through the canonical view, never from `metadata` directly.**
Migration `infra/migrations/020_canonical_message_view.sql` created
`v_messages`, `v_message_lines`, `v_chat_title_collisions`,
`v_messages_without_author`. `v_message_lines.line` already contains
`ДИМА --> X` or `X --> ДИМА` plus a `peer_key` that includes `chat_id`.
Reconstructing direction by hand is now forbidden — the view did it.

```bash
ssh hetzner-root "docker exec -i vera3-postgres psql -U vera -d vera -t -A -F'\t' \
  -c \"SELECT direction, speaker, addressee, peer_key, line FROM v_message_lines \
       WHERE ts_utc > TIMESTAMP '<cutoff_utc>' AND source='telegram' ORDER BY ts_utc\"" > window.tsv
```

**2. Never filter by `chat_title` alone.** Titles are not unique: «Дмитрий»
is 8 different chats, «Алексей» 5, «Дарья» 2 (Степаненко from HQ and
Белікова from Aromika — the second is not ITSTEP and leaked into a report).
Filter on `peer_key`, or check `v_chat_title_collisions` first.

**3. Run the linter before delivering. Non-zero exit means do not send.**

```bash
node .claude/skills/itstep-report/lint_authorship.js window.tsv draft.md
```

It flags three things: a sentence that attributes speech to a person while
the wording actually matches what DIMA wrote TO them; a person named in the
draft who never appears in the window; and an ambiguous name covering more
than one chat. Name-to-author mapping lives in `people.json` — a new person
in a report needs an entry there, and a linter complaint about an unknown
name is a real finding, not noise.

- **MANDATORY — print DIRECTION, not columns.** A separate `sender` column is
  not enough: it has failed four times (Zee, Дарья, Marina, Алексей), because
  `chat_title` sits next to the message text and the mind collapses "message
  in Alexey's chat" → "Alexey said". Remove the ambiguity from the output
  itself. Every telegram fetch MUST render one directional string per row:
  ```sql
  SELECT to_char(occurred_at+interval '7 hours','HH24:MI')
         || ' · ' ||
         CASE WHEN metadata->>'author_role'='self'
              THEN 'ДИМА --> ' || (metadata->>'chat_title')
              ELSE coalesce(metadata->>'author_label', metadata->>'chat_title')
                   || ' --> ДИМА' END
         || ': ' || <body...> AS line
  ```
  Read `ДИМА --> X` as "Dima wrote to X", and `X --> ДИМА` as "X wrote to
  Dima". Never print a bare `chat_title` column beside the text.
- **MANDATORY — anchor every attributive sentence while drafting, then audit
  the anchors.** A "careful pass" cannot be trusted: on 2026-07-31 the fetch
  was already directional and the draft still reversed two sentences, because
  prose was composed from a plausible story (subordinate sends the director a
  weekly plan; a colleague jokes about a call) laid over correct data. So:
  1. Draft with an inline `[HH:MM]` anchor on EVERY sentence whose subject is
     a named person. No anchor means the sentence is unsourced — delete it.
  2. Then print the audit as an explicit table: sentence | anchor | the raw
     `ДИМА --> X` / `X --> ДИМА` line at that timestamp | ✓ or ✗.
  3. Only then strip the anchors and emit the report.
  If the audit table was not actually produced, the check did not happen —
  never claim it did.
- **Watch the narrative-plausibility trap.** The reversals cluster on
  sentences that sound organizationally natural: subordinates reporting
  upward, colleagues commenting, someone sending a plan or a summary. Dima
  is usually the one issuing plans, priorities and diagnoses. When a sentence
  feels like a natural org-chart action, that is exactly when to re-read the
  arrow instead of trusting it.
- **A chat with only `ДИМА -->` rows means the counterparty stayed silent.**
  Say so ("ответа не было"), never invent their reply or reverse the thread.
- Filter chats to the itstep-relevant set before reading — the raw dump is
  dominated by unrelated chats. Known itstep-relevant `chat_title` values so
  far: `J Branch Internal`, `Дарья`, `Maya`, `citra`, `Vasil CRM`,
  `Jakarta sales`, `J_ID Target`, `Jakarta: sms report`, `KBB`/`Бага`, `Lisa`,
  `Mentor Jakarta Branch`, `Vibe Coding Event`. Known noise to exclude:
  `Старшие и отчеты` (Veranda restaurant finance, not itstep), `Stepan`
  channel's lead-stage-change spam (`Manager X moved the lead to Y`, high
  volume, low signal), `ITS | Tech4You` (a different, unrelated project
  despite the similar name), `Мелентьев` (Veranda/personal — restaurant,
  hotel, performers), `Maria`/`@ivmari` (Veranda: Grab, кальян, Poster).
- **A keyword is not project membership.** A chat entered the 2026-07-31
  report because one message said "реклама" and "карта" — but the chat was
  Veranda's. Before pulling an unfamiliar chat into the report, read its
  PREVIOUS days (`ORDER BY occurred_at DESC LIMIT 25`) and confirm the
  counterparty actually works on itstep. Words that look itstep-ish but
  aren't project-specific: реклама, карта, бюджет, оплата, кабинет, лид.
- Stepan dev activity: `gh api repos/zapleoceo/stepan2/commits?since=<ISO
  timestamp of previous report's cutoff>`, reversed to chronological order.
- **Timezone / "what day is today" check**: this session's injected
  `currentDate` can drift from real elapsed time (long-running background
  work). Before deciding which calendar day is "today," confirm against
  `SELECT max(occurred_at) FROM events WHERE source='telegram'` (that's UTC;
  Jakarta local is UTC+7) rather than trusting the system prompt's date.
