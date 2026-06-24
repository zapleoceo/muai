# DB Schema Design: Ready Subtypes

## Problem Statement

Current "ready" status is monolithic — no distinction between:
- **Ready 4 Deal**: Lead has contact, wants to proceed with course purchase (sales conversion flow)
- **Ready 4 OpenHouse**: Lead interested in attending Open House 29 Juni (event attendance flow)

These require different:
- Notification templates (Telegram alerts vary)
- Admin panel UI (different action buttons, deadlines)
- Follow-up workflows (purchase vs. attendance confirmation)
- Lead scoring/metrics

---

## Design Decision: Option 1 — `ready_subtype` Column

**Chosen over alternatives** for:
- **Minimal schema change** — one VARCHAR column, backward compatible
- **Deterministic at source** — can infer from context (chat, signal keywords)
- **Queryable & indexable** — standard SQL patterns work
- **Future-proof** — can add subtypes without migration (e.g., `ready_callback`, `ready_demo`)

### Rejected alternatives:
- **Option 2** (boolean `ready_for_openhouse`): Adds second state tracking; unmaintainable with >2 subtypes
- **Option 3** (stage enum `ready_deal`, `ready_openhouse`): Conflates status with stage; breaks "ready" as a distinct lifecycle state

---

## Schema Changes

### New column in `events` table

```sql
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS ready_subtype VARCHAR(20);
  
-- Enum-like check (optional, for safety)
-- ready_subtype IN (NULL, 'deal', 'openhouse')
```

**Column properties:**
- `VARCHAR(20)` — allows: `null`, `'deal'`, `'openhouse'`, future extensible
- `DEFAULT NULL` — leads without explicit ready status (backward compat)
- `NOT NULL` when `triage_metadata->>'needs_action' = true` AND classified as ready

**Canonical subtypes:**
- `null` — not ready (default for non-ready events)
- `'deal'` — ready to BUY (lead has contact, clear intent to purchase, within cohort)
- `'openhouse'` — ready to ATTEND (interested in June 29 Open House event)

### Index

```sql
CREATE INDEX IF NOT EXISTS ix_events_ready_subtype 
  ON events (ready_subtype)
  WHERE triage_metadata->>'needs_action' = 'true';
```

Fast queries for dashboard: `SELECT * FROM events WHERE ready_subtype='deal' ORDER BY occurred_at DESC`

---

## Backward Compatibility

### Existing rows

**All current ready rows (if they exist) default to `'deal'` subtype:**

```sql
UPDATE events 
SET ready_subtype = 'deal'
WHERE triage_metadata->>'needs_action' = 'true'
  AND ready_subtype IS NULL;
```

**Rationale:** 
- Primary use case is course sales (deal flow)
- Open House is a new workflow (no existing rows)
- Conversion flow is default behavior

### Inference from metadata

If events were previously marked ready via a flag, the LLM-triaged `triage_metadata` should include `"ready_signal_type"` to help categorize:

```json
{
  "needs_action": true,
  "ready_signal_type": "contact_with_purchase_intent",  // → 'deal'
  "ready_signal_type": "event_attendance_interest",     // → 'openhouse'
  ...
}
```

**Seeding script** (one-time backfill):

```sql
UPDATE events 
SET ready_subtype = CASE
  WHEN triage_metadata->>'ready_signal_type' ILIKE '%purchase%' THEN 'deal'
  WHEN triage_metadata->>'ready_signal_type' ILIKE '%attendance%' THEN 'openhouse'
  ELSE 'deal'  -- fallback default
END
WHERE triage_metadata->>'needs_action' = 'true'
  AND ready_subtype IS NULL;
```

---

## ORM Model Update (`vera_shared/db/models.py`)

Add to `EventRow`:

```python
class EventRow(Base):
    """..."""
    
    # [existing fields...]
    
    # Ready status subtype
    ready_subtype: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default=None,
    )
```

**No additional indices in ORM** — define in SQL migration for clearer version control.

---

## Manager Dashboard Changes

### Admin panel: Separate tabs or filter

**Option A — Separate tabs (recommended):**
```
┌──────────────────────────────────┐
│ Ready Leads                      │
├──────────────────────────────────┤
│ [All]  [Ready 4 Deal]  [OpenHouse] │  ← buttons
├──────────────────────────────────┤
│ Lead Name    | Status | Action    │
│ Aleksandra   | deal   | [Call]    │
│ Boris        | deal   | [Message] │
│ Catalina     | ohouse | [Confirm] │
│ Denis        | ohouse | [Reminder]│
└──────────────────────────────────┘
```

**Option B — Inline filter:**
```
Filter: [Ready Status] ▼ (All | Deal | OpenHouse)
```

### Action buttons vary by subtype

| Subtype | Buttons | Purpose |
|---------|---------|---------|
| `'deal'` | `[Call]` `[Message]` `[Send Payment Link]` `[Mark Paid]` | Conversion flow |
| `'openhouse'` | `[Send Invite]` `[Confirm Attendance]` `[Add to Guest List]` `[Set Reminder]` | Event flow |

### Query in backend

```python
# FastAPI route for dashboard
@app.get("/api/leads/ready")
async def get_ready_leads(subtype: str | None = None):
    """Filter by subtype: 'deal', 'openhouse', or None for all ready."""
    async with get_session() as s:
        stmt = select(EventRow).where(
            EventRow.triage_metadata['needs_action'].astext == 'true'
        )
        if subtype:
            stmt = stmt.where(EventRow.ready_subtype == subtype)
        rows = await s.scalars(stmt.order_by(EventRow.occurred_at.desc()))
        return [row.to_dict() for row in rows]
```

---

## Telegram Notification Templates

### Per-subtype notification logic

**In TG bot notifier (e.g., `ingestor-telegram` or `bot-telegram`):**

```python
async def notify_ready_lead(event: EventRow, manager_id: int):
    """Send alert to manager; format varies by ready_subtype."""
    
    if event.ready_subtype == 'deal':
        text = (
            f"🔥 READY 4 DEAL\n\n"
            f"{event.content_text[:200]}\n\n"
            f"Lead has contact + purchase intent.\n"
            f"Action: Call or send payment link."
        )
        buttons = [
            InlineKeyboardButton("📞 Call", callback_data=f"call:{event.id}"),
            InlineKeyboardButton("💳 Payment Link", callback_data=f"pay:{event.id}"),
        ]
    
    elif event.ready_subtype == 'openhouse':
        text = (
            f"🏠 READY 4 OPEN HOUSE (June 29)\n\n"
            f"{event.content_text[:200]}\n\n"
            f"Lead wants to attend. Confirm attendance & add to guest list."
        )
        buttons = [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{event.id}"),
            InlineKeyboardButton("📧 Send Invite", callback_data=f"invite:{event.id}"),
        ]
    
    else:  # fallback
        text = f"Ready lead:\n\n{event.content_text[:200]}"
        buttons = []
    
    await tg_client.send_message(
        chat_id=manager_id,
        text=text,
        reply_markup=InlineKeyboardMarkup([buttons])
    )
```

### Triage prompt enhancement

Update `worker.py` TRIAGE_PROMPT_TEMPLATE to include:

```
ready_subtype: <null | 'deal' | 'openhouse'>
  - 'deal': lead has contact info + clear purchase intent + ready to proceed
  - 'openhouse': lead interested in June 29 Open House event (not purchase)
  - null: not ready or ambiguous
```

Then postprocess:

```python
def postprocess_triage(parsed: dict[str, Any], source: str) -> dict[str, Any]:
    """..."""
    
    # Validate ready_subtype
    ready_subtype = parsed.get("ready_subtype", "").strip().lower() or None
    if ready_subtype and ready_subtype not in ("deal", "openhouse"):
        ready_subtype = None
    parsed["ready_subtype"] = ready_subtype
    
    return parsed
```

---

## Migration Script

### SQL Migration: `004_ready_subtype.sql`

```sql
-- Migration 004: ready_subtype classification
-- Separates "ready" leads into sales vs. event flows
--
-- Subtypes: null | 'deal' | 'openhouse'
--
-- Apply via:
--   docker exec vera3-postgres psql -U vera -d vera -f /tmp/004.sql

BEGIN;

-- Add column
ALTER TABLE events
  ADD COLUMN IF NOT EXISTS ready_subtype VARCHAR(20) DEFAULT NULL;

-- Seed backward-compat defaults
-- All current ready events (if any) → 'deal' (primary sales flow)
UPDATE events 
SET ready_subtype = 'deal'
WHERE triage_metadata->>'needs_action' = 'true'
  AND ready_subtype IS NULL;

-- Index for dashboard queries
CREATE INDEX IF NOT EXISTS ix_events_ready_subtype 
  ON events (ready_subtype)
  WHERE triage_metadata->>'needs_action' = 'true';

-- Comment (self-documenting)
COMMENT ON COLUMN events.ready_subtype IS
  'Lead ready status subtype: null (not ready) | deal (ready to buy) | openhouse (ready to attend event)';

COMMIT;
```

### Apply in production

```bash
# On server: hetzner-root
ssh -p 9617 hetzner-root

docker exec vera3-postgres psql -U vera -d vera <<'EOF'
BEGIN;

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS ready_subtype VARCHAR(20) DEFAULT NULL;

UPDATE events 
SET ready_subtype = 'deal'
WHERE triage_metadata->>'needs_action' = 'true'
  AND ready_subtype IS NULL;

CREATE INDEX IF NOT EXISTS ix_events_ready_subtype 
  ON events (ready_subtype)
  WHERE triage_metadata->>'needs_action' = 'true';

COMMIT;
EOF

echo "Migration 004 complete."
```

---

## Implementation Checklist

- [ ] **1. SQL Migration**
  - [ ] Create `vera3/infra/migrations/004_ready_subtype.sql`
  - [ ] Review with `psql` dry-run
  - [ ] Apply to production

- [ ] **2. ORM Update**
  - [ ] Add `ready_subtype` field to `EventRow` in `models.py`
  - [ ] Type hint: `str | None`

- [ ] **3. Triage Logic**
  - [ ] Update `TRIAGE_PROMPT_TEMPLATE` with ready_subtype rules
  - [ ] Update `postprocess_triage()` to validate/normalize subtype
  - [ ] Add test in `test_triage_classify.py`

- [ ] **4. TG Notification**
  - [ ] Update notifier in `bot-telegram` or webhook handler
  - [ ] Template per subtype (deal vs. openhouse)
  - [ ] Action buttons vary by subtype

- [ ] **5. Admin Dashboard**
  - [ ] Add filter/tabs for `ready_subtype`
  - [ ] Display subtype badge in lead list
  - [ ] Action buttons context-aware (vary by subtype)
  - [ ] Query: `SELECT * FROM events WHERE ready_subtype = ?`

- [ ] **6. Tests**
  - [ ] Unit: triage → correct subtype classification
  - [ ] Integration: end-to-end lead → notification → dashboard

- [ ] **7. Documentation**
  - [ ] Update `VERA.md` domain model section
  - [ ] Add comment to migration script
  - [ ] Explain to manager: how to see leads by type in dashboard

---

## Metrics & Monitoring

**Dashboard KPIs by subtype:**

```sql
-- Weekly ready leads by subtype
SELECT 
  ready_subtype,
  COUNT(*) as count,
  COUNT(CASE WHEN occurred_at >= NOW() - INTERVAL '7 days' THEN 1 END) as past_7d
FROM events
WHERE triage_metadata->>'needs_action' = 'true'
GROUP BY ready_subtype;

-- Conversion by subtype (if tracking in separate conversion_status column)
SELECT 
  ready_subtype,
  COUNT(*) as ready_count,
  SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) as converted,
  ROUND(100.0 * SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) / COUNT(*), 1) as conversion_pct
FROM events
WHERE triage_metadata->>'needs_action' = 'true'
GROUP BY ready_subtype;
```

---

## Future Extensions

Without schema changes, can add:
- `ready_action_deadline` (TIMESTAMP) — when to follow up
- `ready_urgency` (VARCHAR) — high | medium | low
- `ready_notes` (TEXT) — manager notes per lead

Store in `triage_metadata` JSON as interim before deserving separate columns.

Example:
```json
{
  "needs_action": true,
  "ready_subtype": "deal",
  "ready_action_deadline": "2026-06-25T17:00:00Z",
  "ready_urgency": "high",
  "ready_notes": "Called at 2pm, will send payment link later"
}
```

---

## Summary

| Aspect | Decision |
|--------|----------|
| **Storage** | `events.ready_subtype VARCHAR(20)` |
| **Subtypes** | `null` \| `'deal'` \| `'openhouse'` |
| **Backward compat** | Existing ready → `'deal'` |
| **Queryability** | Index on `(ready_subtype)` where needs_action=true |
| **Notifications** | Template varies per subtype + buttons |
| **Admin UI** | Tabs/filter + context-sensitive actions |
| **Migration** | `004_ready_subtype.sql` one-time seed |
| **Extensibility** | Add subtypes or deadlines without schema change |
