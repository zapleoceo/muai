"""
PostToolUse/Write|Edit hook — after editing any static/*.js or static/index.html,
checks that:
1. All onclick/onchange handlers in index.html are exposed on window in app.js
2. All window.settings.* in app.js have matching exports in settings.js

Exits 2 (block) if broken references found.
"""
import json
import re
import sys
from pathlib import Path

data = json.load(sys.stdin)
path = str(
    data.get("tool_input", {}).get("file_path")
    or data.get("tool_input", {}).get("path")
    or ""
)

STATIC = Path(__file__).parent.parent.parent / "static"

if not any(part in path for part in ("static/", "static\\")):
    sys.exit(0)

# ── load files ────────────────────────────────────────────────────────────────
def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

html    = read(STATIC / "index.html")
app_js  = read(STATIC / "app.js")
sjs     = read(STATIC / "pages" / "settings.js")

# ── 1. HTML handlers vs window exports ───────────────────────────────────────
html_fns = set(re.findall(r'on(?:click|change)="([a-zA-Z_]\w*)\(', html))
window_fns = set(re.findall(r'window\.([a-zA-Z_]\w*)\s*=', app_js))
missing_window = html_fns - window_fns

# ── 2. window.settings.* vs settings.js exports ──────────────────────────────
settings_via_window = set(re.findall(r'window\.\w+\s*=\s*settings\.([a-zA-Z_]\w*)', app_js))
settings_exports = set(re.findall(r'^export\s+(?:async\s+)?function\s+([a-zA-Z_]\w*)', sjs, re.MULTILINE))
missing_exports = settings_via_window - settings_exports

errors = []
if missing_window:
    errors.append(f"HTML handlers not on window: {', '.join(sorted(missing_window))}")
if missing_exports:
    errors.append(f"window.settings.* missing in settings.js: {', '.join(sorted(missing_exports))}")

if errors:
    print(json.dumps({
        "decision": "block",
        "message": "❌ JS reference check failed:\n" + "\n".join(f"  • {e}" for e in errors),
    }))
    sys.exit(2)
