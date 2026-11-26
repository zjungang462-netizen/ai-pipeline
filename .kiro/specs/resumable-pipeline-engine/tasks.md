# Tasks: Resumable Pipeline Engine

- [x] 1. Define core contracts (`StageSpec`, `RunContext`, `Result`, `StageStatus`) — `contracts.py` _(R1)_
- [x] 2. Implement serial stage state machine with failure stop — `engine.py` _(R1)_
- [x] 3. Add human approval gate and `approve` command — `engine.py`, `cli.py` _(R1)_
- [x] 4. Persist run state and per-stage state — `state.py`; tests: `test_state_backup.py`, `test_state_migration.py` _(R2, R4)_
- [x] 5. Implement `(inputs_hash, outputs_hash)` completion and downstream stale marking — `state.py`, `review_policy.py` _(R2)_
- [x] 6. Isolate attempts and switch `current` only on success — `state.py` _(R3)_
- [x] 7. Implement `resume`, `rerun_from`, and interrupted-wait recovery — `engine.py`, `deferred_recovery.py` _(R4)_
- [x] 8. Pin base SHA, archive artifacts, clean worktrees — `archive.py` _(R3)_
- [x] 9. Budget pre-check and usage settlement — `budget.py`; tests: `test_budget.py` _(R5)_
- [x] 10. Zero-key showcase exercising approval + resume — `kiroween-demo.example.toml`
