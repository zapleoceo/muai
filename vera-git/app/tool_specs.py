from vera_shared.tools.spec import ToolParam, ToolSpec

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="git_status",
        description="Run `git status --porcelain` on the project repo. Returns "
                    "list of modified/added/deleted files.",
        params=[],
    ),
    ToolSpec(
        name="git_log",
        description="Recent commits on the project repo. Returns list of "
                    "{hash, author, date, message}.",
        params=[ToolParam("limit", "integer", "How many commits.",
                          required=False, default=10)],
    ),
    ToolSpec(
        name="git_diff",
        description="Show working-tree diff (uncommitted). Returns text diff.",
        params=[ToolParam("path", "string", "Restrict to one file/dir.",
                          required=False, default="")],
    ),
    ToolSpec(
        name="deploy_status",
        description="Check production deployment state: last commit on server, "
                    "running containers, last build success.",
        params=[],
    ),
    ToolSpec(
        name="deploy_trigger",
        description="Trigger a production deploy: git pull + docker compose "
                    "build + up. Returns deployment result.",
        params=[],
    ),
]
