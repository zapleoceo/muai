"""
PreToolUse/Write|Edit hook — warns when a file exceeds the 200-line limit.
Informational only (exit 0) so it never blocks, just prints a warning.
"""
import json
import os
import sys

LIMIT = 200

data = json.load(sys.stdin)
path = (
    data.get("tool_input", {}).get("file_path")
    or data.get("tool_input", {}).get("path")
    or ""
)

if path and os.path.isfile(path):
    lines = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = sum(1 for _ in f)
    except OSError:
        pass
    if lines > LIMIT:
        print(
            json.dumps({
                "decision": "warn",
                "message": (
                    f"⚠️  {os.path.basename(path)} is {lines} lines "
                    f"(limit {LIMIT}). Consider splitting into smaller modules."
                ),
            })
        )
