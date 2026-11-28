# AI Pipeline Specification (v0.5.10)

## Overview
AI Pipeline (`agent-orchestrator`) is a local-first, evidence-driven multi-AI development workflow orchestrator built with zero external runtime dependencies (Python 3.11+ standard library) and an embedded web workbench (`orchestrator/dashboard`).

## Core Architecture & Requirements
1. **Multi-Stage Resumable Pipeline (`orchestrator/engine.py`, `orchestrator/state.py`)**
   - Supports 1–12 configurable serial stages (`report`, `code`, `test`, `review`) with per-node model/tool assignment, human approval gates, and rollback targets.
   - Input fingerprinting and attempt archiving ensure deterministic resumption via `run-id` without silently reusing stale artifacts.
2. **Evidence-First Verification (`orchestrator/evidence.py`, `orchestrator/review_policy.py`)**
   - `code` stages require non-zero net repository changes tracked against literal file manifests.
   - `test` stages execute deterministic `argv` commands directly without LLM mediation; process exit codes serve as verification evidence.
   - `review` stages produce structured `review_verdict.json` outputs requiring zero unresolved issues to pass.
3. **Unified Multi-Model & CLI Adapter Layer (`orchestrator/adapters/`)**
   - Supports OpenAI-compatible HTTP APIs alongside local authenticated coding CLIs (Claude Code, Codex, Qwen, Qoder, Doubao, Grok, Antigravity, DSH) with pre-flight version gates (`verified_runtimes.py`).
4. **Multi-Agent Discussion & Web Workbench (`orchestrator/collaboration/`, `orchestrator/dashboard/`)**
   - Provides streaming multi-agent group discussion, arena job comparisons, budget/telemetry insights, and a local web dashboard (`server.py` + `static/`).

5. **Zero-Key Reproducible Showcase (`agent-orchestrator/kiroween-demo.example.toml`)**
   - Combines `FakeAdapter` report/code stages with real Python `test` execution and a human approval gate so reviewers can verify full pipeline orchestration offline without external credentials.
