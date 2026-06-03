from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    token: Mapped[str] = mapped_column(String, nullable=False)
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=1500)
    daily_used: Mapped[int] = mapped_column(Integer, default=0)
    # Per-key cost cap. NULL = unlimited (only request-count caps apply).
    # When set, is_available() blocks the key once daily_cost_used_usd
    # reaches this value. Resets at the same time as daily_used.
    daily_cost_limit_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_cost_used_usd: Mapped[float] = mapped_column(Float, default=0.0)
    daily_reset_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    required_caps: Mapped[list] = mapped_column(JSON, default=list)
    http_url: Mapped[str] = mapped_column(String, nullable=False)
    bot_username: Mapped[str | None] = mapped_column(String, nullable=True)
    tools: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="offline")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_text: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    agents_used: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    final_result: Mapped[str | None] = mapped_column(String, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tokens_used: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    transport: Mapped[str] = mapped_column(String, default="stdio")
    command: Mapped[list | None] = mapped_column(JSON, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    env: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="stopped")
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    tools_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls_count: Mapped[int] = mapped_column(Integer, default=0)
    last_tool_call_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    installed_by: Mapped[str] = mapped_column(String, default="manual")  # manual|self_extend
    auth_state: Mapped[str] = mapped_column(String, default="ok")  # ok|token_expired
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DecisionReplay(Base):
    """Fast lookup of past user decisions keyed by sender. Lets triage
    surface 'repeat last time' as a one-tap option instead of waiting
    for the LLM to derive it from graph retrieval (which is noisy and
    rate-limited). Updated on each record_user_decision."""
    __tablename__ = "decision_replays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False, index=True)
    sender_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    tool: Mapped[str | None] = mapped_column(String, nullable=True)
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingFollowup(Base):
    """Tracks 'Свой ответ' click → next DM message routes to that event,
    with a 5-min TTL. Survives vera-core restart so user can switch
    tabs / restart bot freely."""
    __tablename__ = "pending_followups"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MCPProposal(Base):
    """Self-extension flow state. Each row tracks one proposal: needed
    capability → candidate package → owner decision → install result."""
    __tablename__ = "mcp_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capability: Mapped[str] = mapped_column(String, nullable=False)
    package_name: Mapped[str | None] = mapped_column(String, nullable=True)
    package_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    env_required: Mapped[list | None] = mapped_column(JSON, nullable=True)
    env_collected: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="proposed")
    # proposed → awaiting_creds → installing → active | rejected | failed | uninstalled
    source_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mcp_server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Trigger(Base):
    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    predicate: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    triage_prompt: Mapped[str | None] = mapped_column(String, nullable=True)
    auto_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    fired_count: Mapped[int] = mapped_column(Integer, default=0)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GmailAccount(Base):
    __tablename__ = "gmail_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    refresh_token_enc: Mapped[str] = mapped_column(String, nullable=False)
    access_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    access_expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    history_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    include_automated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Source(Base):
    """Configurable event source (gmail account, telegram identity, etc).
    All polling behaviour, filters and per-source thresholds live here."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String, nullable=False)        # gmail|telegram|bank|...
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=120)
    base_threshold: Mapped[float] = mapped_column(Float, default=0.95)
    filters: Mapped[list | None] = mapped_column(JSON, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    intake_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str] = mapped_column(String, nullable=False, default="generic")
    content_text: Mapped[str | None] = mapped_column(String, nullable=True)
    content_extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    entity_hints: Mapped[list | None] = mapped_column(JSON, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    graphiti_episode_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    triage_status: Mapped[str] = mapped_column(String, default="pending")
    triage_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BackfillJob(Base):
    """Resumable backfill of one source from a given date. The runner
    walks `source.backfill(since)` and writes events; on crash the cursor
    survives so the next process picks up where we stopped."""
    __tablename__ = "backfill_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    since: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|running|done|error
    cursor: Mapped[str | None] = mapped_column(String, nullable=True)  # source-specific resume token
    events_ingested: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TgDialog(Base):
    """Cache of Telegram dialogs (chats/users/groups/channels) so
    search_dialogs is instant — no iter_dialogs scan per request.
    Refreshed by a background loop in vera-telegram."""
    __tablename__ = "tg_dialogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # peer_id
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # user|bot|group|supergroup|channel
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    folders: Mapped[list | None] = mapped_column(JSON, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                     onupdate=datetime.utcnow)


class IngestJob(Base):
    """Queued deep-extraction job. Cheap deterministic edges are written
    synchronously at /event time; this row defers the expensive LLM
    entity extraction so the API stays fast."""
    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|running|done|error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IgAccount(Base):
    """Instagram account connected to Vera. One row per account.
    Access token stored encrypted (same pattern as GmailAccount)."""
    __tablename__ = "ig_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    access_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    business_account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=300)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_dm_cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="disconnected")  # disconnected|ok|error
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IgAutoReply(Base):
    """Auto-reply rule for Instagram DMs. When an incoming DM for an account
    contains ALL trigger_keywords (case-insensitive), Vera auto-replies with
    response_template without going through full triage."""
    __tablename__ = "ig_auto_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_username: Mapped[str] = mapped_column(String, nullable=False, index=True)
    trigger_keywords: Mapped[list] = mapped_column(JSON, default=list)  # ["цена", "курс"]
    response_template: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    last_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
