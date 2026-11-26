# Requirements: Evidence-First Verification

## Introduction
A stage must never be marked complete just because an LLM says "done". Every stage type has a concrete, machine-checkable form of evidence.

## Requirements

### R1. Code stage evidence
**User story:** As a reviewer, I want proof that a code stage actually changed the repository.
- WHEN a `code` stage finishes with an empty net repository diff THEN the system SHALL mark the stage failed.
- Changed files SHALL be recorded in a literal file manifest for the attempt.

### R2. Test stage evidence
**User story:** As a reviewer, I want tests to be run by the OS, not described by a model.
- `test` stages SHALL execute configured `argv` commands directly, with no LLM in the loop.
- The stage SHALL pass only when the process exit code is 0; stdout/stderr SHALL be archived as evidence.

### R3. Review stage evidence
- `review` stages SHALL produce a structured `review_verdict.json`.
- A review SHALL pass only when the verdict has zero unresolved findings.
- WHEN a review fails THEN the system SHALL feed the findings back to the configured rework target, within a bounded number of rounds.

### R4. Sealed artifacts
- Each attempt's outputs SHALL be sealed into a manifest of content digests.
- Resume, approval and caching SHALL re-validate the manifest; any mismatch SHALL invalidate the attempt.
- An approval SHALL be bound to the exact manifest that was approved.

### R5. Write guards
- An adapter SHALL NOT write outside its allowed workspace; out-of-bounds writes SHALL be recorded as violations and fail the stage.
