# Ready Subtypes — Implementation Guide

## 1. Update Triage Classifier (brain-triage/worker.py)

### Step 1: Enhance TRIAGE_PROMPT_TEMPLATE

In `vera3/services/brain-triage/src/brain_triage/worker.py`, update the prompt:

```python
TRIAGE_PROMPT_TEMPLATE = """Ты — Вера, цифровая память Димы. Прочитай событие и извлеки структуру.

Контекст Димы (его текущая жизнь):
- Branch Director IT STEP Academy Jakarta с апреля 2026 (проект itstep)
- Переезд в Индонезию, виза, KPI команды
- Совладелец бара Veranda во Вьетнаме (проект veranda)
- Жена Маша, дочь Лиза (family)
- Босс Дмитрий Егоров (yegorov@itstep.org)

Событие (источник={source}, account={account}, occurred_at={occurred_at}):
---
{content}
---

Верни СТРОГО JSON по схеме:
{{
  "importance": <0-100, насколько Дима должен это видеть>,
  "project": "<РОВНО ОДНО из: itstep | veranda | family | personal | news | other>",
  "nature": "<РОВНО ОДНО из: world_event | my_intent>",
  "topics": [<2-4 тега: русский, нижний регистр, 1-2 слова>],
  "people_mentioned": [<упомянутые люди>],
  "signals": [
    {{"type": "task|event|news|offer|question|decision|anomaly",
      "summary": "<краткое>",
      "date": "<ISO дата если есть, иначе null>"}}
  ],
  "needs_action": <true/false>,
  "ready_subtype": <null | "deal" | "openhouse" — см. ниже>,
}}

Правило ready_subtype (заполни ТОЛЬКО если needs_action=true):
- "deal": лид ИМЕЕТ контакт И ЯВНОЕ намерение купить курс И готов действовать ЧАС/ДЕНЬ
  Примеры: "Привет, я хочу записаться на курс. Вот мой номер: +62812..."
           "Готов платить, когда начнём?"
           "Как записаться? Дайте счёт."
  
- "openhouse": лид заинтересован ПОСЕТИТЬ Open House 29 июня (НЕ покупка, это мероприятие)
  Примеры: "Подойдёт ли мне курс? Я на Опен Хаусе узнаю подробнее?"
           "Когда у вас опен хаус? Хочу прийти 29 июня"
           "Расскажите про мероприятие на 29 числа"
  
- null (если needs_action=false ИЛИ если готовность неясна)
  Примеры: лид просто спрашивает про программу (информационный запрос)
           лид высказывает сомнения или еще не готов

ВАЖНО: только JSON, без префиксов и комментариев."""
```

### Step 2: Update postprocess_triage()

In the same file, enhance the validation function:

```python
def postprocess_triage(parsed: dict[str, Any], source: str) -> dict[str, Any]:
    """Валидация LLM-классификации против словарей + override по source."""
    
    # ─ Existing validations ─────────────────────────────────────────
    nature = NATURE_BY_SOURCE.get(source) or str(parsed.get("nature") or "").strip()
    if nature not in VALID_NATURES:
        nature = "world_event"
    project = str(parsed.get("project") or "").lower().strip()
    if project not in PROJECT_VOCAB:
        project = "other"
    parsed["nature"] = nature
    parsed["project"] = project
    
    # ─ NEW: Validate ready_subtype ──────────────────────────────────
    ready_subtype = parsed.get("ready_subtype")
    if isinstance(ready_subtype, str):
        ready_subtype = ready_subtype.strip().lower()
    if ready_subtype not in (None, "deal", "openhouse"):
        ready_subtype = None
    # Enforce: ready_subtype can only be set if needs_action=true
    if not parsed.get("needs_action"):
        ready_subtype = None
    parsed["ready_subtype"] = ready_subtype
    
    return parsed
```

### Step 3: Update the _process_one_with_sem to pass ready_subtype to DB

The `process_pending()` function already passes `triage_metadata` to the DB. Ensure it includes ready_subtype:

```python
async def process_pending() -> int:
    """Захватить batch, эмбедить parallel, триаж concurrent, UPDATE."""
    rows = await _claim_batch()
    if not rows:
        return 0

    # ... existing code ...

    async with get_session() as s:
        for (event_id, status, metadata, error), embedding in zip(results, embeddings):
            if status == "pending":
                # ... existing code ...
            elif status == "done":
                await s.execute(
                    update(EventRow).where(EventRow.id == event_id).values(
                        triage_status="done",
                        triage_metadata=metadata,
                        importance=metadata.get("importance") if metadata else None,
                        nature=metadata.get("nature") if metadata else None,
                        project=metadata.get("project") if metadata else None,
                        ready_subtype=metadata.get("ready_subtype") if metadata else None,  # ← NEW
                        embedding_voyage_3=embedding,
                        triage_started_at=None,
                    )
                )
                processed += 1
            else:  # error
                # ... existing code ...
```

---

## 2. Update Telegram Notifier

### Location: likely `bot-telegram/src/bot_telegram/notifier.py` or webhook handler

Add a function to build subtype-aware notifications:

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def notify_ready_lead(
    event_id: int,
    content: str,
    ready_subtype: str | None,
    manager_id: int,
    tg_client,
) -> None:
    """Send alert to manager; template varies by ready_subtype."""
    
    if ready_subtype == "deal":
        # ─ Sales conversion flow ────────────────────────────────────
        text = (
            "🔥 <b>READY 4 DEAL</b>\n\n"
            f"<code>{content[:200]}</code>\n\n"
            "<i>Лид готов купить курс. Контакт известен, намерение ясно.</i>\n"
            "⏱ <b>Action:</b> позвонить или отправить счёт."
        )
        buttons = [
            [
                InlineKeyboardButton("📞 Call", callback_data=f"ready_call:{event_id}"),
                InlineKeyboardButton("💳 Payment", callback_data=f"ready_pay:{event_id}"),
            ],
            [
                InlineKeyboardButton("✋ Pass", callback_data=f"ready_skip:{event_id}"),
            ],
        ]
    
    elif ready_subtype == "openhouse":
        # ─ Event attendance flow ────────────────────────────────────
        text = (
            "🏠 <b>READY 4 OPEN HOUSE</b> — June 29\n\n"
            f"<code>{content[:200]}</code>\n\n"
            "<i>Лид хочет посетить Open House и узнать подробнее.</i>\n"
            "⏱ <b>Action:</b> отправить приглашение и добавить в гостлист."
        )
        buttons = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"ready_ohouse_yes:{event_id}"),
                InlineKeyboardButton("📧 Send Invite", callback_data=f"ready_ohouse_send:{event_id}"),
            ],
            [
                InlineKeyboardButton("❌ Not interested", callback_data=f"ready_skip:{event_id}"),
            ],
        ]
    
    else:
        # ─ Fallback (should not happen if filter is correct) ────────
        text = f"📝 Ready lead:\n\n<code>{content[:200]}</code>"
        buttons = [[InlineKeyboardButton("View", callback_data=f"view:{event_id}")]]
    
    try:
        await tg_client.send_message(
            chat_id=manager_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        log.error(f"Failed to notify manager {manager_id}: {e}")
```

### Integration in webhook/ingestor

When an event is marked ready by triage, the notifier calls:

```python
# In bot-telegram webhook handler or triage result processor
if event_row.triage_metadata.get("needs_action") and event_row.ready_subtype:
    await notify_ready_lead(
        event_id=event_row.id,
        content=event_row.content_text,
        ready_subtype=event_row.ready_subtype,
        manager_id=OWNER_TELEGRAM_ID,  # or multi-manager config
        tg_client=tg_client,
    )
```

---

## 3. Admin Dashboard Query

### Backend route (FastAPI)

Add or update `vera3/services/dashboard/src/api/leads.py`:

```python
from fastapi import FastAPI, Query
from sqlalchemy import select

app = FastAPI()

@app.get("/api/ready-leads")
async def get_ready_leads(subtype: str | None = Query(None)):
    """
    Fetch all ready leads, optionally filtered by subtype.
    
    Query params:
    - subtype: null (all) | 'deal' | 'openhouse'
    
    Returns: [{id, content_text, ready_subtype, occurred_at, ...}]
    """
    async with get_session() as s:
        stmt = select(EventRow).where(
            (EventRow.triage_metadata['needs_action'].astext == 'true')
        )
        
        if subtype:
            if subtype not in ("deal", "openhouse"):
                raise ValueError("Invalid subtype")
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
                "account": row.account,
            }
            for row in rows
        ]


@app.get("/api/ready-leads/stats")
async def get_ready_stats():
    """Summary: count of ready leads by subtype."""
    async with get_session() as s:
        stmt = select(
            EventRow.ready_subtype,
            func.count(EventRow.id).label("count"),
        ).where(
            EventRow.triage_metadata['needs_action'].astext == 'true'
        ).group_by(EventRow.ready_subtype)
        
        rows = await s.execute(stmt)
        return {
            row[0] or "unknown": row[1]
            for row in rows
        }
        # Example return: {"deal": 5, "openhouse": 3, "unknown": 1}
```

### Frontend display

In dashboard React/Vue component:

```jsx
// Example React component
import { useEffect, useState } from 'react';

export function ReadyLeads() {
  const [leads, setLeads] = useState([]);
  const [filter, setFilter] = useState(null); // 'deal', 'openhouse', or null
  
  useEffect(() => {
    const params = filter ? `?subtype=${filter}` : '';
    fetch(`/api/ready-leads${params}`)
      .then(r => r.json())
      .then(setLeads);
  }, [filter]);
  
  const buttonsBySubtype = {
    deal: (id) => (
      <>
        <button className="btn-call" onClick={() => callLead(id)}>📞 Call</button>
        <button className="btn-pay" onClick={() => sendPayment(id)}>💳 Payment</button>
      </>
    ),
    openhouse: (id) => (
      <>
        <button className="btn-confirm" onClick={() => confirmAttendance(id)}>✅ Confirm</button>
        <button className="btn-invite" onClick={() => sendInvite(id)}>📧 Send Invite</button>
      </>
    ),
  };
  
  return (
    <div className="ready-leads">
      <h2>Ready Leads</h2>
      <div className="filter-tabs">
        <button className={filter === null ? 'active' : ''} onClick={() => setFilter(null)}>
          All
        </button>
        <button className={filter === 'deal' ? 'active' : ''} onClick={() => setFilter('deal')}>
          🔥 Ready 4 Deal ({leads.filter(l => l.ready_subtype === 'deal').length})
        </button>
        <button className={filter === 'openhouse' ? 'active' : ''} onClick={() => setFilter('openhouse')}>
          🏠 Ready 4 OpenHouse ({leads.filter(l => l.ready_subtype === 'openhouse').length})
        </button>
      </div>
      
      <table>
        <thead>
          <tr>
            <th>Lead</th>
            <th>Type</th>
            <th>Message</th>
            <th>Date</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {leads.map(lead => (
            <tr key={lead.id}>
              <td>{lead.source}#{lead.id}</td>
              <td>
                {lead.ready_subtype === 'deal' && '🔥 Deal'}
                {lead.ready_subtype === 'openhouse' && '🏠 OpenHouse'}
              </td>
              <td className="truncate">{lead.content}</td>
              <td>{new Date(lead.occurred_at).toLocaleString()}</td>
              <td className="actions">
                {buttonsBySubtype[lead.ready_subtype]?.(lead.id)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

---

## 4. Test Cases

### Unit test: triage postprocess (test_triage_classify.py)

```python
import pytest
from brain_triage.worker import postprocess_triage

class TestReadySubtype:
    
    def test_ready_deal_preserved(self):
        parsed = {
            "needs_action": True,
            "ready_subtype": "deal",
            "nature": "world_event",
            "project": "itstep",
        }
        result = postprocess_triage(parsed, source="telegram")
        assert result["ready_subtype"] == "deal"
    
    def test_ready_openhouse_preserved(self):
        parsed = {
            "needs_action": True,
            "ready_subtype": "openhouse",
            "nature": "world_event",
            "project": "itstep",
        }
        result = postprocess_triage(parsed, source="telegram")
        assert result["ready_subtype"] == "openhouse"
    
    def test_ready_subtype_normalized_to_lowercase(self):
        parsed = {
            "needs_action": True,
            "ready_subtype": "DEAL",  # uppercase
            "nature": "world_event",
            "project": "itstep",
        }
        result = postprocess_triage(parsed, source="telegram")
        assert result["ready_subtype"] == "deal"
    
    def test_ready_subtype_cleared_if_not_needs_action(self):
        parsed = {
            "needs_action": False,
            "ready_subtype": "deal",  # should be cleared
            "nature": "world_event",
            "project": "itstep",
        }
        result = postprocess_triage(parsed, source="telegram")
        assert result["ready_subtype"] is None
    
    def test_ready_subtype_invalid_becomes_null(self):
        parsed = {
            "needs_action": True,
            "ready_subtype": "invalid_type",
            "nature": "world_event",
            "project": "itstep",
        }
        result = postprocess_triage(parsed, source="telegram")
        assert result["ready_subtype"] is None
    
    def test_ready_subtype_null_if_missing(self):
        parsed = {
            "needs_action": True,
            "nature": "world_event",
            "project": "itstep",
            # no ready_subtype key
        }
        result = postprocess_triage(parsed, source="telegram")
        assert result["ready_subtype"] is None
```

### Integration test: end-to-end (test_auth.py or new test_ready_workflow.py)

```python
@pytest.mark.asyncio
async def test_ready_deal_flow():
    """Lead says 'I want to buy, here's my contact' → should be classified as deal."""
    
    # Simulate event from Telegram
    event_content = (
        "Привет! Я хочу купить курс Python. "
        "Вот мой номер: +62812345678, "
        "зовут Петр. Когда можем начать?"
    )
    
    event_row = EventRow(
        id=1,
        source="telegram",
        source_event_id="tg_msg_12345",
        account="user_123",
        content_text=event_content,
        occurred_at=datetime.utcnow(),
    )
    
    # Triage (in real flow, this is LLM-based)
    metadata = await triage_one(event_row)
    
    # Should be classified as ready deal
    assert metadata["needs_action"] is True
    assert metadata["ready_subtype"] == "deal"
    assert metadata["project"] == "itstep"


@pytest.mark.asyncio
async def test_ready_openhouse_flow():
    """Lead asks about Open House → should be classified as openhouse."""
    
    event_content = (
        "Привет! Я слышал про Open House 29 июня. "
        "Могу ли я туда прийти? Хочу узнать про курсы."
    )
    
    event_row = EventRow(
        id=2,
        source="instagram_dm",
        source_event_id="ig_msg_99999",
        account="user_456",
        content_text=event_content,
        occurred_at=datetime.utcnow(),
    )
    
    metadata = await triage_one(event_row)
    
    # Should be classified as ready openhouse
    assert metadata["needs_action"] is True
    assert metadata["ready_subtype"] == "openhouse"
    assert metadata["project"] == "itstep"
```

---

## 5. Deployment Checklist

1. **Code changes** (all files):
   ```
   vera3/services/brain-triage/src/brain_triage/worker.py
   vera3/shared/vera_shared/db/models.py
   vera3/services/bot-telegram/src/bot_telegram/notifier.py  (or webhook)
   vera3/services/dashboard/src/api/leads.py  (or similar)
   vera3/tests/unit/test_triage_classify.py  (add test cases)
   ```

2. **Database migration**:
   ```bash
   docker exec vera3-postgres psql -U vera -d vera -f /var/www/vera/vera3/infra/migrations/004_ready_subtype.sql
   ```

3. **Redeploy containers**:
   ```bash
   docker compose down
   docker compose up -d --scale brain-triage=2  # rebuild with new models.py
   ```

4. **Verify**:
   ```bash
   # Check column exists
   docker exec vera3-postgres psql -U vera -d vera -c "SELECT column_name FROM information_schema.columns WHERE table_name='events' AND column_name='ready_subtype';"
   
   # Test LLM classification (send test event)
   # Check TG notification sent to manager with correct buttons
   # Visit admin dashboard → verify filter tabs show ready leads
   ```

---

## 6. Backward Compatibility Guarantees

✅ Existing code paths remain unchanged:
- `needs_action` boolean still works (ready_subtype is derived, not replacing it)
- Queries `WHERE triage_metadata->>'needs_action' = 'true'` unchanged
- TG notifications default to generic template if ready_subtype is NULL

✅ One-time seed updates all existing ready events → 'deal' (no data loss)

✅ ORM: `ready_subtype` is nullable, safe to read as `event.ready_subtype or None`

✅ Frontend: filter gracefully handles NULL (shows all ready if subtype not specified)

---

## 7. Future Enhancements (Post-Launch)

- Add `ready_deadline` column (when to follow up) → sync to manager's calendar
- Add `ready_notes` JSON field (manager annotations per lead)
- Implement auto-escalation (if no action taken by deadline, send reminder TG message)
- Metrics dashboard: conversion rate by subtype, time-to-conversion
- Export: ready leads → CSV for CRM integration
