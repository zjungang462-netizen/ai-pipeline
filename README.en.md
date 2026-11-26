# AI Pipeline — Multi-Agent Development Workflow Orchestrator

> **Kiroween 2026 Submission (`Frankenstein` Category)** — Stitching heterogeneous AI models, local coding CLIs (Claude Code, Codex, Qwen, Qoder, Doubao, Grok, Antigravity), deterministic test runners, and an embedded web workbench into a resumable, evidence-driven engineering pipeline.

## Why AI Pipeline?

Most multi-agent wrappers stop at chat-style handoffs and trust an LLM when it says *"I'm done."* **AI Pipeline (`agent-orchestrator` v0.5.9)** enforces engineering rigor across every stage (`report` -> `code` -> `test` -> `review`):

- **Heterogeneous Model & CLI Stitching**: Mix OpenAI-compatible HTTP endpoints (DeepSeek, Qwen/DashScope, Volcengine Ark, etc.) with authenticated local coding CLIs in a single pipeline.
- **Evidence-First Verification**:
  - `code` stages require non-zero repository diffs tracked by literal file manifests.
  - `test` stages execute real `argv` commands directly (zero LLM mediation); exit codes serve as proof.
  - `review` stages produce structured `review_verdict.json` reports requiring zero unresolved findings.
- **Deterministic Checkpointing & Recovery**: Input fingerprinting, attempt archiving, and `run-id` state persistence allow safe resumption after interruptions or human approval gates without resetting budgets.
- **Embedded Web Workbench**: Local-first HTTP/SSE dashboard (`orchestrator/dashboard`) featuring multi-agent group discussion, arena model comparison, budget/token telemetry, and one-click stage approval.
- **Zero External Runtime Dependencies**: Built purely on the **Python 3.11+ standard library**.

## Built with Kiro (`/.kiro`)

This repository includes Kiro configuration artifacts in the root `/.kiro/` directory:
- `/.kiro/specs/pipeline-architecture.md` — Core state machine, evidence verification, and adapter layer specifications.
- `/.kiro/steering/architecture-standards.md` — Zero-dependency runtime constraints, security hygiene, and adapter contracts.
- `/.kiro/hooks/verify-orchestrator-tests.kiro.hook` — Automated test hook triggered on orchestrator or dashboard edits.

## 3-Step Zero-Key Quickstart (Offline Showcase)

Judges and reviewers can run a complete 5-stage pipeline (requirements, design, code generation, real Python test verification, and human approval gate) **without any external API keys or CLI logins**:

```bash
cd agent-orchestrator

# 1. Run static diagnostics on the zero-key showcase config
python -m orchestrator.cli doctor kiroween-demo.example.toml

# 2. Launch the Web Workbench (opens a tokenized localhost session URL)
python -m orchestrator.cli dashboard --config kiroween-demo.example.toml --port 8766

# 3. Or run directly via CLI and inspect status
python -m orchestrator.cli run kiroween-demo.example.toml --task "Build demo module" --run-id kiroween-1
python -m orchestrator.cli approve kiroween-demo.example.toml --run-id kiroween-1 --stage acceptance
python -m orchestrator.cli resume kiroween-demo.example.toml --run-id kiroween-1
```

To run with live OpenAI-compatible APIs (DeepSeek / Qwen / Ark), see `agent-orchestrator/demo/run_api_demo.py` or `agent-orchestrator/README.md`.

## Repository Layout

```text
ai-pipeline/
├── .kiro/                      # Kiro specs, steering rules, and automated test hooks
├── agent-orchestrator/         # Core engine, adapters, web dashboard, tests, and scripts
│   ├── orchestrator/
│   │   ├── adapters/           # Unified OpenAI-compatible & multi-CLI adapters
│   │   ├── collaboration/      # Group discussion, arena jobs, telemetry & storage
│   │   └── dashboard/          # Zero-dependency HTTP/SSE server & Web UI
│   ├── tests/                  # 600+ unit, contract, and reliability tests
│   └── kiroween-demo.example.toml
├── docs/archive/               # Historical audit reports and validation logs
├── LICENSE                     # MIT License
├── README.md                   # Chinese documentation & development entry
└── README.en.md                # English overview & Kiroween quickstart
```

## License

Released under the [MIT License](LICENSE).
