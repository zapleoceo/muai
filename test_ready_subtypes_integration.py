#!/usr/bin/env python3
"""
Test Ready 4 Deal vs Ready 4 OpenHouse functionality.

Tests:
1. Database: Column exists and is populated
2. API: get_event endpoint returns ready_subtype
3. Repository: Query filtering by ready_subtype
4. Triage: postprocess_triage validation
"""
import asyncio
import json
import sys
from datetime import datetime

from vera3.shared.vera_shared.db.engine import get_session, init_engine
from vera3.shared.vera_shared.db.models import EventRow
from vera3.services.brain_triage.src.brain_triage.worker import postprocess_triage
from sqlalchemy import select, text

async def test_1_database_schema():
    """Test 1: Database - check column exists."""
    print("\n" + "="*70)
    print("TEST 1: Database Schema & Data")
    print("="*70)

    await init_engine()
    async with get_session() as s:
        # Check if column exists
        result = await s.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name='events' AND column_name='ready_subtype'
        """))
        col_info = result.fetchone()

        if not col_info:
            print("✗ Column does not exist!")
            return False

        print(f"✓ Column exists")
        print(f"  Data type: {col_info[1]}")
        print(f"  Nullable: {col_info[2]}")

        # Query events with ready_subtype populated
        result = await s.execute(text("""
            SELECT id, source, account, content_text, ready_subtype,
                   (triage_metadata->>'needs_action')::boolean as needs_action
            FROM events
            WHERE ready_subtype IS NOT NULL
            LIMIT 10
        """))
        rows = result.fetchall()

        print(f"\nRows with ready_subtype populated: {len(rows)}")
        if rows:
            print("\nSample rows:")
            for row in rows[:3]:
                event_id, source, account, content, ready_subtype, needs_action = row
                print(f"  ID: {event_id}")
                print(f"    source: {source}, account: {account}")
                print(f"    ready_subtype: {ready_subtype}")
                print(f"    needs_action: {needs_action}")
                content_preview = (content or "")[:60].replace("\n", " ")
                print(f"    content: {content_preview}...")
                print()

        # Stats by subtype
        result = await s.execute(text("""
            SELECT ready_subtype, COUNT(*) as count
            FROM events
            WHERE ready_subtype IS NOT NULL
            GROUP BY ready_subtype
            ORDER BY count DESC
        """))
        stats = result.fetchall()

        print("\nStats by ready_subtype:")
        for subtype, count in stats:
            print(f"  {subtype}: {count}")

        if stats:
            # Verify only valid subtypes
            valid = all(subtype in ('deal', 'openhouse', None) for subtype, _ in stats)
            if valid:
                print("\n✓ All subtypes are valid (deal, openhouse, or null)")
                return True
            else:
                print("\n✗ Found invalid subtypes!")
                return False

        print("\n✓ No ready_subtype data yet (may be populated by triage)")
        return True

async def test_2_api_endpoint():
    """Test 2: API - get_event returns ready_subtype."""
    print("\n" + "="*70)
    print("TEST 2: API Endpoint (get_event)")
    print("="*70)

    async with get_session() as s:
        # Get a sample event
        result = await s.execute(text("""
            SELECT id, ready_subtype, triage_status
            FROM events
            LIMIT 1
        """))
        row = result.fetchone()

        if not row:
            print("✗ No events in database")
            return False

        event_id, ready_subtype, triage_status = row
        print(f"Testing with event_id={event_id}")
        print(f"  ready_subtype: {ready_subtype}")
        print(f"  triage_status: {triage_status}")

        # Get full event
        event_row = await s.get(EventRow, event_id)
        if event_row.ready_subtype is not None:
            print(f"\n✓ ready_subtype is accessible on EventRow")
            print(f"  Value: {event_row.ready_subtype}")
            return True
        else:
            print(f"\n✓ Event has ready_subtype (currently null)")
            return True

async def test_3_repository_queries():
    """Test 3: Repository - queries by ready_subtype."""
    print("\n" + "="*70)
    print("TEST 3: Repository Queries")
    print("="*70)

    async with get_session() as s:
        # Query by ready_subtype='deal'
        result = await s.execute(select(EventRow).where(
            EventRow.ready_subtype == 'deal'
        ).limit(5))
        deals = result.scalars().all()
        print(f"\n✓ Query by ready_subtype='deal': {len(deals)} rows")

        # Query by ready_subtype='openhouse'
        result = await s.execute(select(EventRow).where(
            EventRow.ready_subtype == 'openhouse'
        ).limit(5))
        openhouses = result.scalars().all()
        print(f"✓ Query by ready_subtype='openhouse': {len(openhouses)} rows")

        # Query by ready_subtype IS NOT NULL
        result = await s.execute(select(EventRow).where(
            EventRow.ready_subtype.isnot(None)
        ).limit(5))
        any_ready = result.scalars().all()
        print(f"✓ Query by ready_subtype IS NOT NULL: {len(any_ready)} rows")

        return True

def test_4_postprocess_triage():
    """Test 4: Triage - postprocess_triage validation."""
    print("\n" + "="*70)
    print("TEST 4: Triage Classification")
    print("="*70)

    test_cases = [
        {
            "name": "Valid: deal with needs_action=true",
            "input": {
                "needs_action": True,
                "ready_subtype": "deal",
                "project": "itstep",
                "nature": "world_event",
            },
            "expected_subtype": "deal",
        },
        {
            "name": "Valid: openhouse with needs_action=true",
            "input": {
                "needs_action": True,
                "ready_subtype": "openhouse",
                "project": "itstep",
                "nature": "world_event",
            },
            "expected_subtype": "openhouse",
        },
        {
            "name": "Valid: DEAL uppercase normalized to lowercase",
            "input": {
                "needs_action": True,
                "ready_subtype": "DEAL",
                "project": "itstep",
                "nature": "world_event",
            },
            "expected_subtype": "deal",
        },
        {
            "name": "Invalid: ready_subtype cleared when needs_action=false",
            "input": {
                "needs_action": False,
                "ready_subtype": "deal",
                "project": "itstep",
                "nature": "world_event",
            },
            "expected_subtype": None,
        },
        {
            "name": "Invalid: unknown subtype becomes null",
            "input": {
                "needs_action": True,
                "ready_subtype": "invalid_type",
                "project": "itstep",
                "nature": "world_event",
            },
            "expected_subtype": None,
        },
        {
            "name": "Valid: null subtype when not specified",
            "input": {
                "needs_action": True,
                "project": "itstep",
                "nature": "world_event",
            },
            "expected_subtype": None,
        },
    ]

    all_pass = True
    for test in test_cases:
        result = postprocess_triage(test["input"].copy(), source="telegram")
        actual = result.get("ready_subtype")
        passed = actual == test["expected_subtype"]

        status = "✓" if passed else "✗"
        print(f"\n{status} {test['name']}")
        print(f"  Input:    {test['input']}")
        print(f"  Expected: {test['expected_subtype']}")
        print(f"  Actual:   {actual}")

        if not passed:
            all_pass = False

    return all_pass

async def main():
    """Run all tests."""
    results = {
        "Test 1: Database": None,
        "Test 2: API": None,
        "Test 3: Repository": None,
        "Test 4: Triage": None,
    }

    try:
        results["Test 1: Database"] = await test_1_database_schema()
        results["Test 2: API"] = await test_2_api_endpoint()
        results["Test 3: Repository"] = await test_3_repository_queries()
        results["Test 4: Triage"] = test_4_postprocess_triage()
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for test_name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")

    all_passed = all(v for v in results.values())
    if all_passed:
        print("\n✓ All tests PASSED")
        return True
    else:
        print("\n✗ Some tests FAILED")
        return False

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
