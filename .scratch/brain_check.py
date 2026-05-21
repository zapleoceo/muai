import asyncio


async def main() -> None:
    from app.graph.client import get_graphiti
    c = await get_graphiti()
    print("Search 'игнорируй verandamybot':")
    r = await c.search(query="игнорируй verandamybot", num_results=10)
    for x in r[:10]:
        print(" -", getattr(x, "fact", None) or getattr(x, "name", None) or str(x)[:120])
    print("\nSearch 'VerandaBot чек':")
    r = await c.search(query="VerandaBot чек стол офик", num_results=10)
    for x in r[:10]:
        print(" -", getattr(x, "fact", None) or getattr(x, "name", None) or str(x)[:120])
    print("\nSearch 'игнорировать игнорить ignore':")
    r = await c.search(query="игнорировать игнорить ignore skip", num_results=10)
    for x in r[:10]:
        print(" -", getattr(x, "fact", None) or getattr(x, "name", None) or str(x)[:120])


asyncio.run(main())
