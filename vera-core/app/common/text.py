"""Small text utilities shared across modules. Eliminates the duplicated
_html_escape implementations."""


def html_escape(s: str | None) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


def truncate(s: str | None, limit: int) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + "…"
