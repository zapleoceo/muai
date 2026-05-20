from app.tools.fetch import web_fetch
from app.tools.search import web_search

HANDLERS = {
    "web_search": lambda query, **_: web_search(str(query)),
    "web_fetch":  lambda url, **_: web_fetch(str(url)),
}
