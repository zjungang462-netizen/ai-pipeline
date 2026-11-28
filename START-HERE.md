# AI Pipeline · Judge & Reviewer Quickstart (`START-HERE.md`)

Welcome! **AI Pipeline** (`agent-orchestrator`, v0.5.10) is our **Kiroween 2026 (`Frankenstein` category)** submission: a zero-dependency multi-agent engineering pipeline and local collaborative workbench that stitches heterogeneous model APIs, local coding CLIs, Docker sandboxes, deterministic test gates, AST/JSON reviews, and hash-locked human approvals into one verifiable delivery loop.

No model API keys, cloud accounts, or third-party `pip` packages are needed to verify this submission.

---

## 1. Prerequisites

- **Python 3.11+** on `PATH` (standard library only; zero `pip install` steps)
- **Git** on `PATH` (used for isolated stage worktrees, net-diff validation, and SHA-256 tree sealing)

---

## 2. One-Command Automated Verification (~5 seconds)

From the repository or extracted archive root, run:

```bash
python -X utf8 agent-orchestrator/scripts/verify_judge_demo.py
```

Expected output:

```text
Judge demo passed: test 1 -> 0, two coding attempts, review passed, approval completed.
```

What this script executes and verifies in a fresh temporary directory using `kiroween-demo.example.toml`:
1. **Intentional Failure & Real Test Gate**: Coding attempt `#1` writes an unnormalized `return value` implementation; a real Python assertion command exits with code `1` (`AssertionError`), and the engine records a `test_feedback` event.
2. **Bounded Repair Loop**: Coding attempt `#2` consumes the persisted test failure and writes `'-'.join(value.lower().split())`; the exact same Python test command re-runs and exits with code `0`.
3. **Independent Scripted AST Review**: The `acceptance` stage inspects `coding_output.py` via Python `ast` without executing untrusted code, writes `review_verdict.json` (`"verdict": "pass"`), seals SHA-256 manifests, and pauses at the human approval gate (`exit code 11`).
4. **Hash-Verified Approval & Idempotent Resume**: `approve --stage acceptance` verifies input/output hashes to complete all 5 stages (`exit code 0`), and a subsequent `resume` confirms completed stages are never re-executed.

---

## 3. Interactive Browser Workbench (English UI + Kiroween Theme)

Launch the local single-user workbench in English:

```bash
cd agent-orchestrator
python -X utf8 -m orchestrator.cli --lang en dashboard --config kiroween-demo.example.toml --port 8766 --open
```

> If port `8766` is busy, choose any free port (for example `--port 8776`) and open the printed `http://127.0.0.1:<port>/?lang=en#token=...` URL.

Inside the browser:
1. Click **`◐ Light`** in the top bar twice to switch to the **`🎃 Kiroween`** theme (or use **`中文 / EN`** anytime to toggle languages without losing state).
2. Click **`⚡ Load the five-stage demo template`** on the welcome screen, then click **`Start demo workflow`**.
3. Inspect **Test verification (`#1` failed -> `#2` passed)**, click **`View code diff ↗`** on **Implementation**, inspect `review_verdict.json` under **Artifact archive**, and click **`Approve & continue`** on **Human acceptance**.

---

## 4. Manual 3-Step CLI Walkthrough (Optional)

If you prefer running each CLI lifecycle command yourself from `agent-orchestrator/`:

```bash
python -X utf8 -m orchestrator.cli --lang en run kiroween-demo.example.toml --run-id demo-01 --task "Implement normalize(value)"
```
*(Note: `run` intentionally exits with code **`11`** when a stage reaches `awaiting_approval` so automation never mistakes an unapproved artifact for a finished delivery.)*

```bash
python -X utf8 -m orchestrator.cli --lang en status kiroween-demo.example.toml --run-id demo-01
python -X utf8 -m orchestrator.cli --lang en approve kiroween-demo.example.toml --run-id demo-01 --stage acceptance
```
*(After `approve`, all 5 stages report `done` and exit with code **`0`**.)*

---

## 5. Kiro Integration & Repository Map

| Path | Purpose |
| --- | --- |
| [`.kiro/specs/`](.kiro/specs/) | Kiro architecture & feature specifications (`pipeline-architecture.md` and module requirement/design/task breakdowns) |
| [`.kiro/steering/architecture-standards.md`](.kiro/steering/architecture-standards.md) | Always-on Kiro steering constraints (zero runtime dependencies, path/env sandboxing, hash-locked gates) |
| [`.kiro/hooks/verify-orchestrator-tests.json`](.kiro/hooks/verify-orchestrator-tests.json) | `PostFileSave` Kiro hook (plus [`.kiro.hook`](.kiro/hooks/verify-orchestrator-tests.kiro.hook) for IDE 0.x) running `verify_judge_demo.py` whenever engine, adapter, or config files change |
| `README.en.md` | Full English architecture overview, adapter matrix, and Docker quickstart |
| `agent-orchestrator/README.md` | Detailed configuration schema & adapter capability tables (drift-checked via `generate_config_docs.py --check`) |
| `agent-orchestrator/DELIVERY.md` | Offline vs. live verification boundaries and release proof criteria |
