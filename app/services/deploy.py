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
        "python",
        "-c",
        "import asyncio; from app.services.plan_executor import ensure_search_infra, ensure_chunk_schema; "
        "async def main(): await ensure_search_infra(); await ensure_chunk_schema(); print('schema OK'); "
        "asyncio.run(main())",
        cwd="/app",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    logger.info("Schema init output:\n%s", out.decode(errors="replace"))


async def run_deploy() -> None:
    prep_cmds = [
        ["git", "-C", "/var/www/tgbot", "pull", "origin", "master"],
        ["docker", "compose", "-f", _COMPOSE_FILE, "build", "bot"],
        [
            "docker",
            "compose",
            "-f",
            _COMPOSE_FILE,
            "run",
            "--rm",
            "bot",
            "python",
            "-c",
            "import asyncio; from app.services.plan_executor import ensure_search_infra, ensure_chunk_schema; "
            "async def main(): await ensure_search_infra(); await ensure_chunk_schema(); print('schema OK'); "
            "asyncio.run(main())",
        ],
    ]
    for cmd in prep_cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        logger.info("Deploy [%s]:\n%s", " ".join(cmd), out.decode(errors="replace"))

    stop_cmd = ["docker", "compose", "-f", _COMPOSE_FILE, "stop", "bot"]
    await asyncio.create_subprocess_exec(
        *stop_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    rm_cmd = ["docker", "compose", "-f", _COMPOSE_FILE, "rm", "-f", "bot"]
    await asyncio.create_subprocess_exec(
        *rm_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    up_cmd = ["docker", "compose", "-f", _COMPOSE_FILE, "up", "-d", "bot"]
    await asyncio.create_subprocess_exec(
        *up_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("Deploy [%s]: launched in new session", " ".join(up_cmd))
