from dataclasses import dataclass, field


@dataclass
class ToolParam:
    name: str
    type: str  # "string" | "integer" | "number" | "boolean"
    description: str
    required: bool = True
    default: object | None = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.type, "description": self.description, "required": self.required}
        if self.default is not None:
            d["default"] = self.default
        return d


@dataclass
class ToolSpec:
    name: str
    description: str
    params: list[ToolParam] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }

    def signature(self) -> str:
        parts = []
        for p in self.params:
            sig = f"{p.name}: {p.type}"
            if not p.required:
                sig += f" = {p.default!r}" if p.default is not None else " = null"
            parts.append(sig)
        return f"{self.name}({', '.join(parts)})"
