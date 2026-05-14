"""
PreToolUse/Bash hook — blocks destructive DB commands without explicit confirmation keyword.
Input: JSON on stdin with {"tool_input": {"command": "..."}}
Output: exit 0 = allow, exit 2 + JSON = block with message.
"""
import json
import re
import sys

DANGEROUS = [
    r"\bDROP\s+TABLE\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bDROP\s+DATABASE\b",
    r"\bDELETE\s+FROM\b(?!.*WHERE)",  # DELETE without WHERE
    r"git\s+push\s+--force",
    r"git\s+reset\s+--hard",
    r"docker\s+compose\s+down\s+-v",   # removes volumes
    r"rm\s+-rf\s+/",
]

data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "")

for pattern in DANGEROUS:
    if re.search(pattern, cmd, re.IGNORECASE):
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"Dangerous command detected: `{pattern}`\n"
                "Double-check this is intentional. If yes, add a comment "
                "# CONFIRMED above the command and retry."
            ),
        }))
        sys.exit(2)
