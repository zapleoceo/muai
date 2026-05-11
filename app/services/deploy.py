import asyncio
import logging

logger = logging.getLogger(__name__)

_COMPOSE_FILE = "/var/www/tgbot/docker-compose.yml"


async def get_logs(lines: int = 200) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", _COMPOSE_FILE,
            "logs", "bot", f"--tail={lines}", "--no-log-prefix",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return stdout.decode(errors="replace")
    except Exception as exc:
        return f"Error fetching logs: {exc}"


async def run_migration() -> None:
    proc = await asyncio.create_subprocess_exec(
        "alembic", "upgrade", "head",
        cwd="/app",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    logger.info("Migration output:\n%s", out.decode(errors="replace"))


async def run_deploy() -> None:
    cmds = [
        ["git", "-C", "/var/www/tgbot", "pull"],
        ["docker", "compose", "-f", _COMPOSE_FILE, "build", "bot"],
        ["docker", "compose", "-f", _COMPOSE_FILE, "up", "-d", "bot"],
    ]
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        logger.info("Deploy [%s]:\n%s", " ".join(cmd), out.decode(errors="replace"))
