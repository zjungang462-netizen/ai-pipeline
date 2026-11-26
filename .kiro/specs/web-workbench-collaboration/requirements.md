# Requirements: Web Workbench & Collaboration

## Introduction
A local-first web workbench to start, watch, compare and approve pipeline runs, plus multi-agent discussion and model arena features, with no third-party server dependencies.

## Requirements

### R1. Local, authenticated server
- The workbench SHALL be served by the standard library HTTP server and bind to loopback by default.
- Requests SHALL require a same-origin session token; unauthenticated requests SHALL get `401 Unauthorized`.
- State-changing commands SHALL go through explicit endpoints and support idempotency keys.

### R2. Live run view
- The UI SHALL stream run events over SSE and show stage status, evidence, artifacts and code diffs.
- Users SHALL be able to approve a waiting stage from the UI.

### R3. Multi-agent discussion
- The system SHALL run a two-role discussion on a fixed snapshot of a run.
- Discussion usage SHALL be accounted separately and SHALL NOT modify historical runs.

### R4. Arena jobs
- Arena comparisons SHALL run as background jobs independent of the HTTP connection.
- Job state and events SHALL be persisted; jobs SHALL be listable and cancellable.

### R5. Insights
- The workbench SHALL provide read-only run summaries, run comparison, paginated failures and exportable run reports.
- Saved templates SHALL NOT carry commands or change permissions.
