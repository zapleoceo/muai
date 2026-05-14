import asyncio
import logging

logger = logging.getLogger(__name__)

_COMPOSE_FILE = "/var/www/tgbot/docker-compose.yml"
_COMPOSE = ["docker", "compose", "-f", _COMPOSE_FILE]


async def _run(*cmd: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}")
    output = stdout.decode(errors="replace")
    if check and proc.returncode not in (0, None):
        raise RuntimeError(f"Command failed (rc={proc.returncode}): {' '.join(cmd)}\n{output}")
    return output


async def get_logs(lines: int = 200) -> str:
    try:
        return await _run(*_COMPOSE, "logs", "bot", f"--tail={lines}", "--no-log-prefix")
    except Exception as exc:
        return f"Error fetching logs: {exc}"


async def run_migration() -> None:
    script = (
        "import asyncio\n"
        "from app.services.plan_executor import ensure_search_infra, ensure_chunk_schema\n"
        "\n"
        "async def main():\n"
        "    await ensure_search_infra()\n"
        "    await ensure_chunk_schema()\n"
        "    print('schema OK')\n"
        "\n"
        "asyncio.run(main())\n"
    )
    proc = await asyncio.create_subprocess_exec(
        "python", "-c", script,
        cwd="/app",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    logger.info("Schema init output:\n%s", out.decode(errors="replace"))


async def run_deploy() -> None:
    logger.info("Deploy: starting")

    pull_out = await _run(
        "git", "-C", "/var/www/tgbot", "pull", "origin", "master",
    )
    logger.info("Deploy [git pull]:\n%s", pull_out)

    build_out = await _run(*_COMPOSE, "build", "bot")
    logger.info("Deploy [build]:\n%s", build_out)

    await _run(*_COMPOSE, "stop", "bot", check=False)
    await _run(*_COMPOSE, "rm", "-f", "bot", check=False)
    await _run(*_COMPOSE, "up", "-d", "bot")

    await asyncio.sleep(15)

    status_out = await _run(*_COMPOSE, "ps", "bot", "--format", "{{.Status}}", check=False)
    status = status_out.strip().lower()
    logger.info("Deploy: container status = %r", status)

    if any(s in status for s in ("restarting", "exited", "unhealthy")):
        logs = await get_logs(60)
        logger.error("Deploy: container failed to start.\n%s", logs)
    else:
        logger.info("Deploy: container is up OK")
