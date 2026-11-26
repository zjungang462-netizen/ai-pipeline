# Design: Web Workbench & Collaboration

## Modules
| Module | Responsibility |
|---|---|
| `dashboard/server.py` | `WorkbenchServer`, `Handler` (`authorised`, `idem`, `stream`, `arena_stream`), `create_server`, `serve` |
| `dashboard/static/` | Single-page UI (themes incl. Kiroween, command palette, stage stepper) |
| `collaboration/storage.py` (`Store`) | SQLite storage for projects, runs, events, messages; stage results still come from run state |
| `collaboration/discussion.py` | `capture` snapshot, `reserve_parent` / `settle_parent` accounting, `execute` |
| `collaboration/arena_jobs.py` | `start_job`, `run_worker`, `list_jobs`, `get_job`, `read_events`, `cancel_job` |
| `collaboration/insights.py` | `run_summary`, `compare`, `failure_page`, `save_template`, `export_run_report` |

## Request flow
```
browser --(token)--> Handler.authorised
   GET  -> read-only views / SSE stream
   POST -> idem(key) -> command -> engine / collaboration service
```

## Design decisions
- Workbench storage is a projection; the engine's run state remains the source of truth.
- Arena workers run out-of-process so a closed tab does not kill a job.
- Loopback + token because the server can execute real OS test processes.
