# Requirements: Unified Adapter Layer

## Introduction
The pipeline must mix OpenAI-compatible HTTP APIs and local coding CLIs in one run, behind one contract, without leaking credentials.

## Requirements

### R1. Single adapter contract
- Every adapter SHALL implement `preflight()` and `run()` from `adapters/base.py` and return a `Result`.
- The engine SHALL NOT depend on any tool-specific output format.

### R2. Declarative option specs
- Each adapter type SHALL declare its options and capabilities in one place (`adapters/spec.py`).
- Config parsing, the factory, `doctor` and the workbench SHALL all use the same spec.
- Option validation SHALL NOT read environment variables or probe the filesystem; environment checks belong to `doctor`.

### R3. OpenAI-compatible API adapter
- SHALL use only `urllib` (no third-party HTTP client) and support SSE streaming.
- API keys SHALL be read only from the environment variable named in config and SHALL never be written to config, logs or artifacts.
- Errors SHALL be classified (e.g. auth, rate limit, server, network).

### R4. CLI adapters
- SHALL support Claude Code, Codex, Qwen, Qoder, Doubao, Grok, Antigravity and DSH.
- Subprocesses SHALL run with an environment allow-list (API keys not passed through), a timeout, and captured stdout/stderr.
- `preflight` SHALL resolve the absolute binary path and record its version.

### R5. Verified container runtimes
- Containerised CLIs SHALL be pinned to an immutable version, image digest and binary SHA-256.
- Docker runs SHALL use shared isolation arguments and SHALL clean up containers.
