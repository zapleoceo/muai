from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

AgentStatus = Literal["online", "offline", "busy"]


@dataclass
class AgentRecord:
    id: str
    name: str
    capabilities: list[str]
    required_caps: list[str]
    http_url: str
    bot_username: str | None = None
    status: AgentStatus = "offline"
    last_heartbeat: datetime | None = None
    registered_at: datetime = field(default_factory=datetime.utcnow)
