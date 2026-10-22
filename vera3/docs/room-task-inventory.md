# Complete Room task inventory

`room_tasks` keeps its existing recent-first, bounded list. For a complete
registry scan, call `room_task_inventory(limit=100)`, then pass each returned
`next_cursor` to the next call. Stop only when `complete=true` and
`returned_count == expected_count`. Include `done` and `cancelled` tasks when
reconciling the registry; classify them separately from unfinished work.

The inventory scans immutable task IDs in key order up to the highest ID seen
on its first call. It does not claim tasks, modify leases, or start work. A
concurrent addition inside this bounded range can make the count disagree;
the response then has `inconsistent=true` and requires a fresh scan. Tasks
created beyond the initial high-water ID appear on the next scan. The cursor
belongs to one Room and is not an authorization token. The registry view is
redacted by the same presentation layer as `room_tasks`.

Completeness means every task present at scan start was enumerated. It does
not prove that an external executor is working or that a task can be resumed.
Those checks belong to later continuation stages.
