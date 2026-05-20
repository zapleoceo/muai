from vera_shared.tools.spec import ToolParam, ToolSpec

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="web_search",
        description=(
            "Search the web for current information via Google Search. Use for "
            "anything that requires fresh facts, news, prices, opening hours, "
            "biographies, definitions, public profiles, or anything outside "
            "Vera's own data sources. Returns a concise summary plus the list "
            "of source URLs that grounded the answer."
        ),
        params=[
            ToolParam("query", "string", "Search query in natural language. Be specific."),
        ],
    ),
    ToolSpec(
        name="web_fetch",
        description=(
            "Download a specific URL and return its readable text (stripped "
            "of HTML, ads, navigation). Use this after web_search when you "
            "need the full content of a particular page. Max 8000 chars returned."
        ),
        params=[
            ToolParam("url", "string", "Absolute http(s) URL."),
        ],
    ),
]
