# Ready Subtypes — Quick Start (TL;DR)

## What Changed

Added `ready_subtype` column to distinguish between two lead flows:
- **'deal'** = Lead wants to BUY course (has contact, clear intent) → Call/Payment buttons
- **'openhouse'** = Lead wants to ATTEND June 29 event → Confirm/Invite buttons

---

## Files Modified/Added

| File | Change | Status |
|------|--------|--------|
| `vera3/infra/migrations/004_ready_subtype.sql` | NEW | ✅ Created |
| `vera3/shared/vera_shared/db/models.py` | Add column to EventRow | ✅ Updated |
| `vera3/services/brain-triage/src/brain_triage/worker.py` | Update LLM prompt + validation | 🔄 TODO |
| `vera3/services/bot-telegram/src/bot_telegram/notifier.py` | Different templates per type | 🔄 TODO |
| `vera3/services/dashboard/src/api/leads.py` | Filter endpoint + UI | 🔄 TODO |
| `vera3/tests/unit/test_triage_classify.py` | Add unit tests | 🔄 TODO |

---

## Deploy Steps (1-2-3)

### 1. Database
```bash
ssh -p 9617 hetzner-root

docker exec vera3-postgres psql -U vera -d vera <<'EOF'
\i /var/www/vera/vera3/infra/migrations/004_ready_subtype.sql
EOF
```

### 2. Rebuild containers
```bash
cd /var/www/vera
docker compose down
docker compose build
docker compose up -d --scale brain-triage=2
```

### 3. Verify
```bash
# Column exists
docker exec vera3-postgres psql -U vera -d vera -c \
  "SELECT column_name FROM information_schema.columns WHERE table_name='events' AND column_name='ready_subtype';"

# Send test event → check TG notification has correct buttons
```

---

## Code Snippets (Copy-Paste Ready)

### 1. Update worker.py — TRIAGE_PROMPT_TEMPLATE

In `TRIAGE_PROMPT_TEMPLATE`, add this to the JSON schema:

```python
  "ready_subtype": <null | "deal" | "openhouse" — см. ниже>,
```

And add rules section:

```python
Правило ready_subtype (заполни ТОЛЬКО если needs_action=true):
- "deal": лид ИМЕЕТ контакт И ЯВНОЕ намерение купить курс
  Примеры: "Привет, я хочу записаться на курс. +62812..."
           "Готов платить, когда начнём?"
  
- "openhouse": лид заинтересован посетить Open House 29 июня
  Примеры: "Расскажите про опен хаус 29 июня?"
           "Когда мероприятие? Хочу прийти."
  
- null: если needs_action=false или готовность неясна
```

### 2. Update worker.py — postprocess_triage()

```python
def postprocess_triage(parsed: dict[str, Any], source: str) -> dict[str, Any]:
    """..."""
    # ... existing code ...
    
    # Validate ready_subtype
    ready_subtype = parsed.get("ready_subtype")
    if isinstance(ready_subtype, str):
        ready_subtype = ready_subtype.strip().lower()
    if ready_subtype not in (None, "deal", "openhouse"):
        ready_subtype = None
    if not parsed.get("needs_action"):
        ready_subtype = None
    parsed["ready_subtype"] = ready_subtype
    
    return parsed
```

### 3. Update worker.py — process_pending()

In the `status == "done"` branch, add:

```python
elif status == "done":
    await s.execute(
        update(EventRow).where(EventRow.id == event_id).values(
            triage_status="done",
            triage_metadata=metadata,
            importance=metadata.get("importance") if metadata else None,
            nature=metadata.get("nature") if metadata else None,
            project=metadata.get("project") if metadata else None,
            ready_subtype=metadata.get("ready_subtype") if metadata else None,  # ← ADD THIS
            embedding_voyage_3=embedding,
            triage_started_at=None,
        )
    )
```

### 4. Update notifier.py

```python
async def notify_ready_lead(
    event_id: int,
    content: str,
    ready_subtype: str | None,
    manager_id: int,
    tg_client,
) -> None:
    """Send alert to manager; template varies by ready_subtype."""
    
    if ready_subtype == "deal":
        text = (
            "🔥 <b>READY 4 DEAL</b>\n\n"
            f"<code>{content[:200]}</code>\n\n"
            "<i>Лид готов купить курс.</i>\n"
            "⏱ <b>Action:</b> позвонить или отправить счёт."
        )
        buttons = [
            [
                InlineKeyboardButton("📞 Call", callback_data=f"ready_call:{event_id}"),
                InlineKeyboardButton("💳 Payment", callback_data=f"ready_pay:{event_id}"),
            ],
        ]
    elif ready_subtype == "openhouse":
        text = (
            "🏠 <b>READY 4 OPEN HOUSE</b> — June 29\n\n"
            f"<code>{content[:200]}</code>\n\n"
            "<i>Лид хочет посетить мероприятие.</i>\n"
            "⏱ <b>Action:</b> отправить приглашение."
        )
        buttons = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"ready_ohouse_yes:{event_id}"),
                InlineKeyboardButton("📧 Send Invite", callback_data=f"ready_ohouse_send:{event_id}"),
            ],
        ]
    else:
        text = f"📝 Ready lead:\n\n<code>{content[:200]}</code>"
        buttons = []
    
    await tg_client.send_message(
        chat_id=manager_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
```

### 5. Dashboard endpoint (FastAPI)

```python
@app.get("/api/ready-leads")
async def get_ready_leads(subtype: str | None = Query(None)):
    """Fetch ready leads, optionally filtered by subtype."""
    async with get_session() as s:
        stmt = select(EventRow).where(
            (EventRow.triage_metadata['needs_action'].astext == 'true')
        )
        if subtype in ("deal", "openhouse"):
            stmt = stmt.where(EventRow.ready_subtype == subtype)
        stmt = stmt.order_by(EventRow.occurred_at.desc())
        rows = await s.scalars(stmt)
        
        return [
            {
                "id": row.id,
                "source": row.source,
                "content": row.content_text[:300],
                "ready_subtype": row.ready_subtype,
                "occurred_at": row.occurred_at.isoformat(),
            }
            for row in rows
        ]
```

---

## Testing Checklist

- [ ] Send test TG message: `"Привет! Хочу купить курс Python. +62812345. Когда начнём?"`
  - Expected: TG notification with "🔥 READY 4 DEAL" + Call/Payment buttons
  
- [ ] Send test TG message: `"Можно ли прийти на опен хаус 29 июня?"`
  - Expected: TG notification with "🏠 READY 4 OPEN HOUSE" + Confirm/Invite buttons
  
- [ ] Visit admin dashboard → Ready Leads tab
  - Expected: see both types, filter buttons work, buttons vary per type
  
- [ ] Query database: `SELECT ready_subtype, COUNT(*) FROM events WHERE triage_metadata->>'needs_action'='true' GROUP BY ready_subtype;`
  - Expected: shows 'deal' and 'openhouse' counts

---

## SQL Queries (Copy-Paste for Monitoring)

```sql
-- All ready leads
SELECT ready_subtype, COUNT(*) FROM events
WHERE (triage_metadata->>'needs_action')::boolean = true
GROUP BY ready_subtype;

-- Deal leads only
SELECT * FROM events WHERE ready_subtype='deal' ORDER BY occurred_at DESC;

-- OpenHouse leads only
SELECT * FROM events WHERE ready_subtype='openhouse' ORDER BY occurred_at DESC;
```

---

## Rollback (If Needed)

```bash
# Remove column (DANGER: data loss)
docker exec vera3-postgres psql -U vera -d vera -c "ALTER TABLE events DROP COLUMN ready_subtype;"

# Or just set all to NULL (safer)
docker exec vera3-postgres psql -U vera -d vera -c "UPDATE events SET ready_subtype=NULL;"

# Revert code changes
git checkout vera3/shared/vera_shared/db/models.py
git checkout vera3/services/brain-triage/src/brain_triage/worker.py
```

---

## Questions?

- **What if LLM returns invalid ready_subtype?** → postprocess_triage() sets to NULL
- **What about old events before migration?** → automatically seeded as 'deal' (primary flow)
- **Can we add more subtypes later?** → Yes, just update prompt + validation
- **What's the UI/UX for managers?** → Tabs or filter dropdown showing counts per type
- **How do we track conversion?** → Store in separate column or triage_metadata JSON

---

## Docs

- Full schema design: `READY_SUBTYPES_SCHEMA_DESIGN.md`
- Implementation details: `READY_SUBTYPES_IMPLEMENTATION.md`
- SQL queries: `READY_SUBTYPES_QUERIES.sql`
