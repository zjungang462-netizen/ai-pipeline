# Tasks: Web Workbench & Collaboration

- [x] 1. Stdlib HTTP/JSON/SSE server with session token auth — `dashboard/server.py` _(R1)_
- [x] 2. Idempotent command endpoints — `dashboard/server.py` _(R1)_
- [x] 3. Live SSE run view, evidence, diff and approve button — `dashboard/static/` _(R2)_
- [x] 4. Graceful shutdown — tests: `test_dashboard_shutdown.py` _(R1)_
- [x] 5. SQLite collaboration store — `collaboration/storage.py` _(R3, R4)_
- [x] 6. Snapshot-based two-role discussion with separate accounting — `collaboration/discussion.py`; tests: `test_streaming_discussion.py` _(R3)_
- [x] 7. Background arena jobs — `collaboration/arena_jobs.py`; tests: `test_arena.py`, `test_arena_jobs.py` _(R4)_
- [x] 8. Insights, failure pagination and report export — `collaboration/insights.py`; tests: `test_pagination_contract.py` _(R5)_
