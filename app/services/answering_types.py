from enum import Enum

from pydantic import BaseModel, Field
from pydantic import model_validator


class PlanStrategy(str, Enum):
    INFO_ONLY = "INFO_ONLY"
    RAG_SEMANTIC = "RAG_SEMANTIC"
    SQL_DATE_SUMMARY = "SQL_DATE_SUMMARY"
    HYBRID = "HYBRID"
    COMMAND = "COMMAND"


class PlanTimeRange(str, Enum):
    NONE = "NONE"
    YESTERDAY = "YESTERDAY"
    TODAY = "TODAY"
    LAST_7_DAYS = "LAST_7_DAYS"
    EXPLICIT = "EXPLICIT"


class PlanScope(str, Enum):
    CURRENT_CHAT = "CURRENT_CHAT"
    ALL_CHATS = "ALL_CHATS"


class PlanToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)


class Plan(BaseModel):
    strategy: PlanStrategy
    tools: list[PlanToolCall] = Field(default_factory=list)
    time_range: PlanTimeRange = PlanTimeRange.NONE
    scope: PlanScope = PlanScope.CURRENT_CHAT
    explicit_from: str | None = None
    explicit_to: str | None = None
    clarify_question: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_explicit_range(self) -> "Plan":
        if self.time_range == PlanTimeRange.EXPLICIT:
            if not self.explicit_from or not self.explicit_to:
                raise ValueError("explicit_from and explicit_to are required when time_range=EXPLICIT")
        return self


class ToolRun(BaseModel):
    name: str
    ok: bool
    meta: dict = Field(default_factory=dict)


class RetrievedContext(BaseModel):
    messages: list[dict] = Field(default_factory=list)
    chunks: list[dict] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    tool_runs: list[ToolRun] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class ReplyResult(BaseModel):
    text: str
    interaction_id: int | None = None
    plan: Plan | None = None
    retrieved: RetrievedContext | None = None
