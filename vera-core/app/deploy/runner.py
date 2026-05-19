import asyncio
import logging

from app.bot.sender import notify_group
from app.deploy.health import health_check

log = logging.getLogger(__name__)
_PROJECT_DIR = "/var/www/vera"
_SERVICES = ["http://vera-core:8000", "http://vera-telegram:8001"]


async def _run(cmd: list[str], cwd: str = _PROJECT_DIR) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode(errors="replace")


async def _get_logs(lines: int = 20) -> str:
    _, out = await _run(["docker", "compose", "logs", "--tail", str(lines)])
    return out


async def deploy(ref: str, message: str) -> None:
    await notify_group(f"🚀 Deploy started\nRef: <code>{ref}</code>\n{message}")

    steps = [
        (["git", "pull"], "git pull"),
        (["docker", "compose", "build"], "docker compose build"),
        (["docker", "compose", "up", "-d"], "docker compose up -d"),
    ]

    for cmd, label in steps:
        code, out = await _run(cmd)
        if code != 0:
            tail = out[-800:] if len(out) > 800 else out
            await notify_group(f"Deploy failed at <b>{label}</b>\n<pre>{tail}</pre>")
            return

    health = await health_check(_SERVICES, timeout=60)
    healthy = all(health.values())
    status = "✅ All services healthy" if healthy else "⚠️ Some services unhealthy: " + str(health)

    logs = await _get_logs(20)
    tail = logs[-800:] if len(logs) > 800 else logs
    await notify_group(f"Deploy complete\n{status}\n<pre>{tail}</pre>")
