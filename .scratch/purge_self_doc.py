import asyncio


async def main() -> None:
    from app.graph.client import get_graphiti
    from app.config import get_settings
    client = await get_graphiti()
    db = get_settings().neo4j_database
    async with client.driver.session(database=db) as s:
        # Remove the workaround episodes; structural fix is in system prompt.
        r = await s.run(
            "MATCH (n:Episodic) WHERE n.name STARTS WITH 'vera-self-doc' "
            "DETACH DELETE n RETURN count(n) AS deleted"
        )
        row = await r.single()
        print("deleted", row["deleted"], "vera-self-doc episodes")


asyncio.run(main())
