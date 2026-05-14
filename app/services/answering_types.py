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
    LAST_30_DAYS = "LAST_30_DAYS"
    ALL_TIME = "ALL_TIME"
    EXPLICIT = "EXPLICIT"


class PlanScope(str, Enum):
    CURRENT_CHAT = "CURRENT_CHAT"
    ALL_CHATS = "ALL_CHATS"


class PlanChatType(str, Enum):
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class PlanToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)

class PlanOnEmpty(str, Enum):
    ASK_CLARIFY = "ASK_CLARIFY"
    RETRY = "RETRY"


class QueryOutputShape(str, Enum):
    ANSWER = "ANSWER"
    LIST = "LIST"
    SUMMARY = "SUMMARY"
    ANALYTICS = "ANALYTICS"


class QueryOperation(str, Enum):
    SEARCH = "SEARCH"
    RECENT_MESSAGES = "RECENT_MESSAGES"
    MEDIA_MESSAGES = "MEDIA_MESSAGES"
    DYNAMIC_QUERY = "DYNAMIC_QUERY"


class QueryActor(str, Enum):
    ANY = "ANY"
    ME = "ME"
    THEM = "THEM"


class QueryPrecisionBias(str, Enum):
    BALANCED = "BALANCED"
    PRECISION = "PRECISION"
    RECALL = "RECALL"


class DynamicFilterOp(str, Enum):
    EQ = "EQ"
    ILIKE = "ILIKE"
    IN = "IN"
    BETWEEN = "BETWEEN"
    IS_NOT_NULL = "IS_NOT_NULL"


class DynamicSelectAgg(str, Enum):
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    MAX = "MAX"
    MIN = "MIN"


class DynamicSelect(BaseModel):
    field: str
    as_name: str | None = None
    agg: DynamicSelectAgg | None = None


class DynamicFilter(BaseModel):
    field: str
    op: DynamicFilterOp
    value: str | int | list[str] | list[int] | None = None
    value_to: str | int | None = None


class DynamicOrder(BaseModel):
    field: str
    desc: bool = True


class DynamicToolSpec(BaseModel):
    select: list[DynamicSelect] = Field(default_factory=list)
    filters: list[DynamicFilter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[DynamicOrder] = Field(default_factory=list)
    limit: int = 50
    require_time_range: bool = True

    @model_validator(mode="after")
    def _validate(self) -> "DynamicToolSpec":
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.limit > 200:
            raise ValueError("limit must be <= 200")
        if not self.select:
            raise ValueError("select must not be empty")
        return self


class QueryConstraints(BaseModel):
    scope: PlanScope = PlanScope.CURRENT_CHAT
    chat_types: list[PlanChatType] | None = None
    chat_ids: list[int] | None = None
    chat_query: str | None = None
    folder: str | None = None

    time_range: PlanTimeRange = PlanTimeRange.NONE
    explicit_from: str | None = None
    explicit_to: str | None = None

    actor: QueryActor = QueryActor.ANY
    media_type: str | None = None
    limit: int | None = None

    @model_validator(mode="after")
    def _validate_explicit_range(self) -> "QueryConstraints":
        if self.time_range == PlanTimeRange.EXPLICIT:
            if not self.explicit_from or not self.explicit_to:
                raise ValueError("explicit_from and explicit_to are required when time_range=EXPLICIT")
        return self


class QueryModel(BaseModel):
    output_shape: QueryOutputShape = QueryOutputShape.ANSWER
    operation: QueryOperation = QueryOperation.SEARCH
    need_proof: bool = False
    precision_bias: QueryPrecisionBias = QueryPrecisionBias.BALANCED
    constraints: QueryConstraints = Field(default_factory=QueryConstraints)

    query_variants: list[str] = Field(default_factory=list)
    subqueries: list[str] = Field(default_factory=list)
    dynamic_tool: DynamicToolSpec | None = None

    clarify_question: str | None = None
    max_steps: int = 1
    on_empty: PlanOnEmpty = PlanOnEmpty.ASK_CLARIFY
    notes: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> "QueryModel":
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_steps > 3:
            raise ValueError("max_steps must be <= 3")
        if self.operation == QueryOperation.RECENT_MESSAGES:
            if not (self.constraints.chat_query or self.constraints.scope == PlanScope.CURRENT_CHAT):
                raise ValueError("RECENT_MESSAGES requires constraints.chat_query (unless scope=CURRENT_CHAT)")
        if self.operation == QueryOperation.MEDIA_MESSAGES:
            if not (self.constraints.media_type or self.constraints.media_type == ""):
                raise ValueError("MEDIA_MESSAGES requires constraints.media_type")
        if self.operation == QueryOperation.DYNAMIC_QUERY:
            if self.dynamic_tool is None:
                raise ValueError("DYNAMIC_QUERY requires dynamic_tool")
        return self


class Plan(BaseModel):
    strategy: PlanStrategy
    tools: list[PlanToolCall] = Field(default_factory=list)
    time_range: PlanTimeRange = PlanTimeRange.NONE
    scope: PlanScope = PlanScope.CURRENT_CHAT
    chat_types: list[PlanChatType] | None = None
    chat_ids: list[int] | None = None
    explicit_from: str | None = None
    explicit_to: str | None = None
    clarify_question: str | None = None
    max_steps: int = 1
    on_empty: PlanOnEmpty = PlanOnEmpty.ASK_CLARIFY
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_explicit_range(self) -> "Plan":
        if self.time_range == PlanTimeRange.EXPLICIT:
            if not self.explicit_from or not self.explicit_to:
                raise ValueError("explicit_from and explicit_to are required when time_range=EXPLICIT")
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_steps > 3:
            raise ValueError("max_steps must be <= 3")
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
