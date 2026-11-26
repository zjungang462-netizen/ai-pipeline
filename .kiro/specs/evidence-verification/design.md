# Design: Evidence-First Verification

## Modules
| Module | Responsibility |
|---|---|
| `orchestrator/evidence.py` | `digest`, `seal`, `validate`, `approved` — one manifest check shared by resume, approval and cache |
| `orchestrator/review_policy.py` (`ReviewPolicy`) | Input fingerprints, review coverage decision, rework feedback, exhausted-review handling |
| `orchestrator/guards.py` (`GuardControls`) | `make_write_guard`, `record_outside_evidence`, persisted violation checks |
| `orchestrator/archive.py` | `record_test_artifacts`, base SHA used to compute the net diff |

## Evidence per stage type
| Stage | Evidence | Pass condition |
|---|---|---|
| `code` | git diff vs pinned base SHA + file manifest | non-zero net change |
| `test` | `argv` exit code, stdout/stderr | exit code == 0 |
| `review` | `review_verdict.json` | zero unresolved findings |
| any | sealed manifest | `evidence.validate` passes |

## Flow
```
adapter.run() -> Result
   -> guards check writes
   -> stage-type evidence check
   -> evidence.seal(attempt) -> outputs_hash
   -> state records (inputs_hash, outputs_hash)
```

## Design decisions
- Evidence is checked by the engine, never by the adapter that produced the work.
- Test commands bypass all model adapters on purpose.
- Review rework is bounded so a disagreeing reviewer cannot loop forever.
