# Requirements: Resumable Pipeline Engine

## Introduction
The engine runs a configurable serial pipeline of 1-12 stages (`report`, `code`, `test`, `review`) and must survive interruptions, human approval pauses and re-runs without silently reusing stale results or resetting budgets.

## Requirements

### R1. Serial stage state machine
**User story:** As a developer, I want stages to run in a fixed order so that each stage consumes verified upstream output.
- WHEN a run starts THEN the engine SHALL execute stages in configured order, one at a time.
- WHEN a stage fails THEN the engine SHALL stop downstream stages and record a failure reason.
- WHEN a stage is configured with an approval gate THEN the engine SHALL pause in `waiting_approval` until `approve` is called for that stage.

### R2. Completion criterion
**User story:** As a developer, I want "done" to mean the inputs and outputs actually match, not that a flag was set.
- A stage SHALL be considered complete only when its recorded `(inputs_hash, outputs_hash)` pair matches the current inputs and the sealed artifact manifest.
- WHEN an upstream stage's outputs change THEN the engine SHALL mark that stage and all downstream stages as stale.

### R3. Attempt isolation
- Each stage execution SHALL write into a new attempt directory.
- The stage's `current` pointer SHALL switch to an attempt only after that attempt succeeds.
- A failed or interrupted attempt SHALL NOT overwrite the last good attempt.

### R4. Resume and re-run
- WHEN `resume` is called with an existing `run-id` THEN the engine SHALL skip stages whose completion criterion still holds and continue from the first incomplete stage.
- WHEN `rerun_from <stage>` is called THEN the engine SHALL invalidate that stage and everything downstream.
- WHEN the process starts and finds an interrupted wait THEN it SHALL recover the run into a consistent state.

### R5. Budget accounting
- The engine SHALL check the call budget before each model invocation and reject the call when it would exceed the limit.
- Reported usage SHALL be settled after each call and SHALL persist across resume.
