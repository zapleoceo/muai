from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    input_text: str
    context: dict
    capability_needed: str


@dataclass
class TaskResult:
    task_id: str
    agent_id: str
    output: str
    success: bool
    error: str | None = None
    tokens_used: dict | None = None
    duration_ms: int = 0
