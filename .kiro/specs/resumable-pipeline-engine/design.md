# Design: Resumable Pipeline Engine

## Modules
| Module | Responsibility |
|---|---|
| `orchestrator/engine.py` (`Engine`) | `prepare`, `run`, `approve`, `rerun_from`, approval checkpoint, deferred / interrupted-wait recovery |
| `orchestrator/state.py` (`RunState`, `StageState`, `AttemptRecord`) | Persisted run state, attempt directories, `mark_stale_downstream`, event log |
| `orchestrator/archive.py` (`ArchiveOperations`) | Pin base SHA per stage, archive artifacts, clean worktrees, record test artifacts |
| `orchestrator/budget.py` (`BudgetPolicy`, `BudgetExceeded`) | `before_call` / `check` / `account_usage` |
| `orchestrator/contracts.py` | `StageSpec`, `RunContext`, `Result`, `StageStatus`, `ErrorKind` — the only contracts the engine depends on |

## State model
```
pending -> running -> succeeded
                   -> failed
                   -> waiting_approval -> (approve) -> succeeded
succeeded -> stale   (upstream inputs/outputs changed)
```
Completion = `(inputs_hash, outputs_hash)`. `inputs_hash` is derived from upstream output hashes plus the stage spec (see `review_policy.ReviewPolicy._inputs_hash`). `outputs_hash` comes from the sealed attempt manifest (`evidence.seal`).

## Attempt layout
```
<state_dir>/<run-id>/
  state.json
  stages/<stage>/attempts/<n>/   # isolated per attempt
  stages/<stage>/current -> attempts/<n>   # switched only on success
```

## Resume flow
1. `RunState.load(run-id)`
2. For each stage: recompute inputs hash; if it matches and `evidence.validate` passes, skip.
3. Otherwise run a new attempt; on success, seal, switch `current`, `mark_stale_downstream`.

## Design decisions
- Engine never parses tool output; adapters return `Result` (contracts.py).
- Budget state is part of the run state, so resume does not reset spend.
