# Listener result ownership and current facts

Help commands open the existing Room task and send one intake notice. They never
run the personal-memory brain as a second final executor. `deliver_answer` also
rejects help finals at its delivery boundary, including after restart.
`claim_unless_accepted` retains Room-first lock ordering and reserves help finals
for the executor before or after handoff acceptance. Dispatch ACK and an offered
handoff are not execution acceptance; the existing fenced Room acceptance remains
authoritative. No new access, source credentials, or periodic publisher is added.

## Delivery and legacy data

`reserve_result` commits the executor reservation and `result_pending` together.
`store_result_receipt` / `record_receipt` commit the actual Telegram message ID,
timestamp, delivery=sent, answered_at, closed help state and done queue state in one
transaction. `hold_command` and polling updates cannot reopen a terminal result.
`stale_result_claim` waits five minutes before marking a stranded reservation
unknown; another live poller cannot immediately invalidate an active sender.
A late real receipt may resolve unknown. A sent receipt cannot be downgraded.

Only proven pre-send/Telegram rejection errors are retryable, up to the existing
bounded limit. Timeout, no positive integer receipt and interrupted send are
unknown and never replayed automatically. The intake notice has a separate
idempotency key and the same conservative error classification.

`review_result` retains evidence as `result_review` when delivery is disabled by
the existing cutoff, final text is absent, source verification fails, execution
ends without delivery, or a legacy brain reservation/receipt exists. A legacy
brain/sent receipt is preserved and the executor's prepared result stays in Room;
no duplicate owner reply is sent and no false no-taker notice follows. This is a
review requirement, not proof of delivery of the executor result. No migration,
historic command replay, fabricated receipt or live incident closure is performed.
`cancel_help` records explicit execution cancellation without claiming delivery.
Source-uncertain tasks still need the existing explicit owner confirmation.

## Current public facts versus personal memory

`external_facts` classifies the bounded current-facts routes (currency, weather,
news), preserving explicit personal recall. `ask_brain` fails closed for these
routes instead of searching old personal events. Normal help intake uses the
existing authorized Room executor. `fresh_facts_action` passes actual UTC reference
time, source/time/type requirements, and the `FRESH_FACTS_V1` final-note contract.
The owner's timezone is not inferred. The ordinary memory agent receives actual
UTC time and its existing configured local offset for relative-date interpretation.

`fresh_result` checks the executor-attested observation, not an independently
fetched live source. Its JSON requires answer, boolean available, timezone-aware
checked_at, and (when available) as_of, HTTPS source_url and rate_type. Checks and data must
be within 30 minutes **at delivery**, never future-dated. Only an explicit
rate_type=official NBU with HTTPS hostname exactly bank.gov.ua permits a daily
observation (up to 24 hours); cash/buy/sell and other sources do not get this exception.
Unavailable sources produce an explicit unavailable result, without substituting
old memory. Official, cash, buy and sell rates must be distinguished by the
executor. A malformed/stale result goes to review without owner delivery. There
is no arbitrary URL fetching, new network credential or paid lookup. This bounded
classifier is not a general natural-language fact verifier.

## Scoped diagnosis

Existing `room_task_history(task_id, room)` includes optional `voice_status` from
`voice_status_for_task`. It requires that the exact Room task was created by Vera,
has one voice_command ref, and matches that queue row's task ID. Other tasks and
rooms return null. It exposes only verification, routing, dispatch ACK, handoff
offer, accepted executor, execution/help/delivery states and real opaque receipt
and time. Unknown provider/model remain null. It does not expose queue listings,
source windows, private events, instructions, secrets or owner Telegram IDs.
Existing Room read authorization and redaction remain in place.

## Verification and release

Unit regressions first reproduce competing final, premature completion, false
closed and lost legacy result. Further cases cover source uncertainty, cutoff,
definite rejection vs timeout/no receipt, duplicate/restart, late hold, stale help,
freshness at delivery, personal recall, and cross-room diagnostic isolation.
PostgreSQL integration exercises handoff acceptance versus attempted brain claim,
parallel executor delivery, a poller during a live send, and late voice-worker
hold after the actual receipt. A passing mock test is not live Telegram delivery.
Release uses the existing code-only master CI process after exact-HEAD review;
unrelated blocked Office/Veranda PRs are excluded. Live health/version can be read
after deployment; a new real owner command is needed for a new live delivery
receipt, rather than replaying a historic command.
