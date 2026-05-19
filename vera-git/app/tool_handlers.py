from app.tools import deploy_status, deploy_trigger, git_diff, git_log, git_status


HANDLERS = {
    "git_status":      lambda **_: git_status(),
    "git_log":         lambda limit=10, **_: git_log(int(limit)),
    "git_diff":        lambda path="", **_: git_diff(path),
    "deploy_status":   lambda **_: deploy_status(),
    "deploy_trigger":  lambda **_: deploy_trigger(),
}
