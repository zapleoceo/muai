import asyncio
from sqlalchemy import select, func
from vera_shared.db.engine import get_session
from vera_shared.db.models import Event


async def main() -> None:
    async with get_session() as s:
        total = (await s.execute(select(func.count()).select_from(Event))).scalar()
        ingested = (await s.execute(
            select(func.count()).select_from(Event)
            .where(Event.graphiti_episode_uuid.isnot(None))
        )).scalar()
        not_ingested = total - (ingested or 0)
        print(f"events total={total} ingested_to_graph={ingested} missing={not_ingested}")

        # Sample of recently NOT ingested
        r = await s.execute(
            select(Event.id, Event.source, Event.triage_status)
            .where(Event.graphiti_episode_uuid.is_(None))
            .order_by(Event.id.desc()).limit(10)
        )
        print("recent NOT ingested (sample):")
        for row in r.all():
            print(" -", dict(row._mapping))

    # Count Graphiti nodes directly
    from app.graph.client import get_graphiti
    c = await get_graphiti()
    from app.config import get_settings
    db = get_settings().neo4j_database
    async with c.driver.session(database=db) as ses:
        for label in ["Episodic", "Entity", "Community"]:
            r = await ses.run(f"MATCH (n:{label}) RETURN count(n) AS c")
            row = await r.single()
            print(f"graph {label}: {row['c']}")
        r = await ses.run(
            "MATCH (n:Episodic) RETURN n.name AS name, "
            "substring(n.content, 0, 60) AS snippet ORDER BY n.created_at DESC LIMIT 8"
        )
        async for row in r:
            print(" episode:", row["name"], "|", row["snippet"])


asyncio.run(main())
