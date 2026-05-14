"""
Stop hook — appends a one-line timestamp + summary to .claude/session_log.txt
so there's a lightweight trail of what changed each session.
"""
import datetime
import json
import os
import sys

data = json.load(sys.stdin) if not sys.stdin.isatty() else {}
summary = data.get("stop_reason", "session ended")

log_path = os.path.join(os.path.dirname(__file__), "..", "session_log.txt")
log_path = os.path.normpath(log_path)

now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
line = f"{now} | {summary}\n"

try:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
except OSError:
    pass
