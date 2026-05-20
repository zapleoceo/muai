import httpx
from bs4 import BeautifulSoup

_MAX_CHARS = 8000
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; VeraBot/1.0; "
        "+https://dima.veranda.my)"
    ),
    "Accept-Language": "en,ru;q=0.9",
}


async def web_fetch(url: str) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, headers=_HEADERS,
    ) as c:
        r = await c.get(url)

    final_url = str(r.url)

    if r.status_code != 200:
        return {
            "url": final_url, "status_code": r.status_code,
            "error": f"http {r.status_code}",
        }

    ctype = r.headers.get("content-type", "")
    if "html" not in ctype.lower():
        return {
            "url": final_url, "status_code": r.status_code,
            "content_type": ctype,
            "text": r.text[:_MAX_CHARS],
        }

    soup = BeautifulSoup(r.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "iframe", "svg",
                     "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = "\n".join(lines)

    return {
        "url": final_url,
        "status_code": r.status_code,
        "title": title,
        "text": cleaned[:_MAX_CHARS],
        "truncated": len(cleaned) > _MAX_CHARS,
    }
