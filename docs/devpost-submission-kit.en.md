# Kiroween 2026 Turnkey Submission Kit (`AI Pipeline`)

This file contains ready-to-copy text for **Devpost**, **YouTube Studio**, and **GitHub**, plus the exact commands to take clean English screenshots, push green CI to GitHub, and build the verified Judge ZIP.

---

## 1. Devpost Basic Fields

- **Project Name**:
  `AI Pipeline — Evidence-First Multi-Agent Workbench`
- **Elevator Pitch (Subtitle, <= 200 chars)**:
  `Zero-dependency Python orchestrator stitching cloud LLM APIs, local coding CLIs, Docker sandboxes, and real OS test gates into an evidence-locked multi-agent workbench.`
- **Hackathon Category / Track**:
  `Frankenstein`
- **Built With Tags**:
  `kiro`, `python`, `claude-code`, `openai-api`, `deepseek`, `qwen`, `docker`, `sqlite`, `server-sent-events`, `git`
- **"Try it out" Links** (add all 3 after pushing & creating the GitHub Release):
  1. `GitHub Repository` -> `https://github.com/zjungang462-netizen/ai-pipeline`
  2. `Judge Quickstart (START-HERE)` -> `https://github.com/zjungang462-netizen/ai-pipeline/blob/main/START-HERE.md`
  3. `Verified Judge Release (ZIP)` -> `https://github.com/zjungang462-netizen/ai-pipeline/releases/latest`
- **Portfolio Profile `Website` Field**:
  `https://github.com/zjungang462-netizen/ai-pipeline`
- **Recommended 4 English Screenshot Captions (in `🎃 Kiroween` theme, `?lang=en`)**:
  1. `English Workbench Home (Kiroween theme): 1-click five-stage zero-key demo launcher`
  2. `Real OS Test Gate: Attempt #1 fails Python assertions (exit code 1) and routes persisted feedback back to coding`
  3. `Bounded Repair Diff: Attempt #2 fixes normalize(value) in an isolated Git worktree with SHA-256 sealing`
  4. `Non-Executing AST Review & Human Approval Gate: Structured review_verdict.json verified before completing all 5 stages`

---

## 2. Devpost Full Project Story (Copy Everything Inside the Markdown Block Below)

````markdown
## Inspiration

Coding agents and local AI CLIs have become remarkably capable at generating patches—yet **an agent exiting with code `0` and saying *"All tests pass!"* is not engineering evidence**. In daily software delivery, developers end up manually copying prompts between cloud APIs for architecture, local CLIs for file edits, terminal windows for test suites, and diff viewers for code review—while losing track of token budgets, stale checkpoints, or which commit was actually tested.

We built **AI Pipeline (`agent-orchestrator`)** for the **Kiroween 2026 `Frankenstein` category** to stitch incompatible AI species and deterministic operating-system primitives into one living, verifiable organism: **no stage advances on model self-report alone, and no unapproved change reaches delivery**.

---

## What It Does (`Frankenstein` Category Fit)

Like classic Frankenstein engineering, **AI Pipeline** bolts together **10 heterogeneous AI execution backends**, **Git worktree isolation**, **real OS test processes**, **non-executing AST/JSON review gates**, **SQLite WAL event streams**, and a **bilingual real-time HTTP/SSE web workbench**—powered strictly by the **Python 3.11+ standard library with zero third-party runtime dependencies (`0` `pip install` packages)**:

1. **Unified Multi-Species Adapter Layer (`10` Runtimes)**
   - **Cloud HTTP/SSE APIs**: `openai_compat` (OpenAI, DeepSeek, Qwen, and compatible endpoints with streaming telemetry).
   - **Local Authenticated Coding CLIs**: `cli_claude` (Claude Code), `cli_codex` (Codex CLI), `cli_qwen` (Qwen Code), `cli_qoder` (Qoder CLI), `cli_agy` (Google Antigravity), and `cli_dsh` (DeepSeek Harness)—each guarded by pre-flight binary/version verification and minimal environment allowlists.
   - **Hardened Container Sandboxes**: `cli_grok` and `cli_doubao` run inside non-root Docker containers (`--cap-drop=ALL`, `--security-opt=no-new-privileges`, read-only rootfs, and explicit `editable_files` workspace sync).
   - **Deterministic Simulation**: `fake` scripted adapters enable instant, reproducible zero-key verification without external accounts or paid API calls.

2. **Evidence-First Pipeline Engine (`1–12` Resumable Stages)**
   - **`code` stages** execute in isolated Git baselines and require **non-empty net diffs** within allowed paths (rejecting `.git` tampering, path traversal, symlinks, and submodules).
   - **`test` stages** bypass LLMs completely—the engine invokes configured OS `argv` commands directly, captures exit codes and output tails, and routes failures back to `code` within bounded repair rounds and stall circuit-breakers.
   - **`review` stages** require a valid structured `review_verdict.json` (`"verdict": "pass"` with zero unresolved blocking issues).
   - **Hash-Locked Human Approval & Deployment**: Checkpoints, `approve`, `resume`, and controlled deployments bind SHA-256 input/output manifests and Git `HEAD` commits so stale approvals can never be replayed after workspace edits.

3. **Embedded Collaborative Workbench (`🎃 Kiroween` Theme + English/Chinese Toggle)**
   - **Task Rooms**: Live SSE timeline, red/green Git diff viewer, artifact inspector, token/duration budgets, and 1-click report export.
   - **Read-Only Multi-Agent Roundtables**: Convene `architect`, `developer`, `skeptic`, and `reviewer` personas on a frozen run snapshot without mutating workspace files.
   - **Best-of-N Parallel Worktree Arena**: Pit two CLI/model candidates against the same task in isolated Git worktrees, run deterministic acceptance tests on both, compare diffs side-by-side, and merge the winner via `git merge --ff-only`.
   - **Historical Route Self-Optimization**: Recommends stage adapters from local empirical pass-rate and token-efficiency history.

---

## Try It in 60 Seconds (Zero API Keys, Zero `pip install`)

**Prerequisites:** Python `3.11+` and `git` on `PATH`.

### 1. One-Command End-to-End Judge Verification (~5 seconds)

From the repository root (or extracted Judge ZIP root), run:

```bash
python -X utf8 agent-orchestrator/scripts/verify_judge_demo.py
```

Expected result:

```text
Judge demo passed: test 1 -> 0, two coding attempts, review passed, approval completed.
```

This runs [`kiroween-demo.example.toml`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/agent-orchestrator/kiroween-demo.example.toml) in an isolated temporary workspace:
1. **Attempt #1 (Intentional Bug)**: Coding writes an unnormalized `return value`; a real Python assertion command exits with code **`1`**, and the engine persists a `test_feedback` event.
2. **Attempt #2 (Bounded Repair)**: Coding reads the persisted test failure and writes `'-'.join(value.lower().split())`; the exact same Python assertion command re-runs and exits with code **`0`**.
3. **Non-Executing AST Review**: The `acceptance` reviewer parses `coding_output.py` via Python's `ast` module without executing untrusted code, writes `review_verdict.json`, and pauses at the human approval gate (**CLI exit code `11`**, ensuring automation never mistakes an unapproved run for completion).
4. **Hash-Verified Approval & Idempotent Resume**: Approving `acceptance` verifies artifact hashes to finish all 5 stages (**exit code `0`**), and resuming reuses completed attempts without re-running them.

### 2. Interactive Browser Workbench (English UI + `🎃 Kiroween` Theme)

```bash
cd agent-orchestrator
python -X utf8 -m orchestrator.cli --lang en dashboard --config kiroween-demo.example.toml --port 8766 --open
```

Click **`◐ Light`** twice in the top bar for the **`🎃 Kiroween`** theme, click **`⚡ Load the five-stage demo template`**, and watch the failure -> repair -> diff -> approval loop live in your browser. *(Note: The zero-key demo uses deterministic scripted adapters to prove engine transitions, real OS assertions, AST review, and hash-locked gates; live API/CLI configs are documented in [`agent-orchestrator/README.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/agent-orchestrator/README.md).)*

---

## How We Built It with Kiro (`/.kiro`)

Kiro's spec-driven workflow, steering guardrails, and agent hooks shaped the entire architecture and kept regressions out of the repository:

1. **Four Multi-Stage Kiro Spec Suites ([`.kiro/specs/`](https://github.com/zjungang462-netizen/ai-pipeline/tree/main/.kiro/specs))**
   Alongside the system blueprint ([`pipeline-architecture.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/.kiro/specs/pipeline-architecture.md)), we decomposed the platform into four full `requirements.md` -> `design.md` -> `tasks.md` specifications:
   - [`resumable-pipeline-engine`](https://github.com/zjungang462-netizen/ai-pipeline/tree/main/.kiro/specs/resumable-pipeline-engine): State machine transitions, SHA-256 input fingerprinting, bounded repair loops, and crash recovery.
   - [`evidence-verification`](https://github.com/zjungang462-netizen/ai-pipeline/tree/main/.kiro/specs/evidence-verification): Non-zero Git net-diff checks, direct `argv` test execution, and `review_verdict.json` schema enforcement.
   - [`adapter-layer`](https://github.com/zjungang462-netizen/ai-pipeline/tree/main/.kiro/specs/adapter-layer): Unified `AdapterSpec` contract, CLI telemetry parsing, stream secret redaction, and Docker sandbox isolation.
   - [`web-workbench-collaboration`](https://github.com/zjungang462-netizen/ai-pipeline/tree/main/.kiro/specs/web-workbench-collaboration): Loopback-authenticated HTTP/SSE server, SQLite WAL projection caches, multi-agent roundtables, and Worktree Arena.

2. **Always-On Architectural Steering ([`.kiro/steering/architecture-standards.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/.kiro/steering/architecture-standards.md))**
   Enforces four non-negotiable rules on every agent turn: (1) **Zero Runtime Dependencies** (Python 3.11+ stdlib only), (2) **Evidence Over Self-Report**, (3) **Adapter Isolation & Pre-Flight Checks**, and (4) **Security & Repository Hygiene** (keeping `/.kiro` tracked while blocking credentials, `.env`, and runtime SQLite state).

3. **Automated `PostFileSave` Verification Hook ([`.kiro/hooks/verify-orchestrator-tests.json`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/.kiro/hooks/verify-orchestrator-tests.json))**
   Whenever an agent edits Python engine/adapter modules, static workbench assets, JS contract tests, or `.example.toml` files, Kiro automatically runs `python -X utf8 agent-orchestrator/scripts/verify_judge_demo.py` (with legacy IDE 0.x support in [`verify-orchestrator-tests.kiro.hook`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/.kiro/hooks/verify-orchestrator-tests.kiro.hook)), guaranteeing the five-stage failure-repair-approval loop never breaks.

---

## Architecture Overview

```text
TOML Workflow Config (10 validated *.example.toml templates)
        │
        ▼
┌───────────────────────────────────────────────────────────────────┐
│ Unified Adapter Layer (Preflight + Stream Secret Redaction)       │
│  • Cloud APIs: openai_compat (HTTP / SSE)                         │
│  • Local CLIs: Claude, Codex, Qwen, Qoder, Antigravity, DSH       │
│  • Sandboxes : Docker non-root Grok & Doubao + Offline Fake       │
└───────────────────────────────┬───────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ Resumable Pipeline Engine (SHA-256 Fingerprints & Stage Locks)    │
│  [report] ──► [code (Git worktree)] ──► [test (OS argv)]          │
│                     ▲                         │                   │
│                     └─── persisted feedback ──┘ (exit != 0)       │
│                                               │ (exit == 0)       │
│                                               ▼                   │
│             [Human Hash-Locked Gate] ◄── [review_verdict.json]    │
└───────────────────────────────┬───────────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│ Local SQLite WAL + Embedded HTTP/SSE Workbench (EN / ZH)          │
│  • Live SSE Rooms & Diff Viewer   • Best-of-N Worktree Arena      │
│  • Multi-Agent Roundtables        • Empirical Adapter Routing     │
└───────────────────────────────────────────────────────────────────┘
```

---

## Challenges We Ran Into

- **Cross-Platform Process Tree & Pipe Containment**: Coding CLIs on Windows (`cmd.exe` wrappers, Job Objects, NTFS `DELETE_PENDING` lock contention) behave very differently from POSIX process groups (`os.killpg`). We built suspended-process Job attachment, incremental UTF-8 stream tail buffers, and deterministic process-group cleanup so timed-out or stalled CLIs never leak orphan processes or hang pipes.
- **Preventing Sandbox & Worktree Escapes**: Merging candidate patches or mounting workspaces into Docker can silently leak host files or follow symlinks. We added strict relative-path guards (blocking `.git` variants, `120000` symlinks, and `160000` submodules), externalized `.pipeline.lock` files so `git status --porcelain` stays clean, and required explicit `editable_files` allowlists for container writes.
- **Non-Blocking Real-Time Observability on Stdlib Only**: Serving live SSE event streams alongside background directory discovery and SQLite writes using only `http.server` and `sqlite3` required WAL session release before entering SSE loops, bounded LRU projection caches, and idempotent command gates.

---

## Accomplishments That We're Proud Of

- **Zero External Runtime Dependencies**: Full orchestrator, 10-adapter layer, SQLite collaboration store, SSE server, Arena, and bilingual UI built strictly on Python 3.11+ stdlib.
- **Rigorous Offline Verification (`928` Tests)**: A **`928`-test offline Python regression suite** (v0.5.10, CI run #71), **`7` Node.js frontend contract suites** (covering pagination, cursor stability, i18n state preservation, and execution scope), **`10` drift-checked example configs**, and dual-run (`quick` + `full`) release packaging (`scripts/release.py`).
- **Stream-Level Credential Hygiene**: Live `StreamRedactor` and `precommit.py` credential scanners scrub secret environment values, bearer tokens, and URL credentials before logs or failure receipts touch disk.

---

## What We Learned & What's Next

Building AI Pipeline taught us that **constraining AI agents with deterministic OS boundaries makes them dramatically more useful**: when specifications (`.kiro/specs`), workspace locks, real test exit codes, and SHA-256 approval gates are enforced outside the prompt, heterogeneous models can collaborate safely without human babysitting on every step.

**What's Next:**
- **DAG Stage Parallelism**: Extending serial stages with fan-out/fan-in dependency graphs over isolated Git worktrees.
- **OpenTelemetry Export**: Streaming per-stage token, latency, and stall-breaker metrics to external observability backends.
- **Authenticated Remote Sandboxes**: Expanding `remote_worker.py` with mTLS and ephemeral container pools for team CI runners.

---

## Repository Guide

- **Judge 60-Second Quickstart**: [`START-HERE.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/START-HERE.md)
- **English Architecture & Walkthrough**: [`README.en.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/README.en.md)
- **Kiro Specs, Steering & Hooks**: [`.kiro/`](https://github.com/zjungang462-netizen/ai-pipeline/tree/main/.kiro)
- **Adapter & Config Schema Reference**: [`agent-orchestrator/README.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/agent-orchestrator/README.md)
- **Offline vs. Live Verification Boundaries**: [`agent-orchestrator/DELIVERY.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/agent-orchestrator/DELIVERY.md)
- **Operations & Recovery Guide**: [`agent-orchestrator/OPERATIONS.md`](https://github.com/zjungang462-netizen/ai-pipeline/blob/main/agent-orchestrator/OPERATIONS.md)
````

---

## 3. YouTube Studio Title & Description (`https://www.youtube.com/watch?v=6LW1qBKOxC4`)

- **Video Title**:
  `AI Pipeline — Kiroween 2026 Demo (Frankenstein Category)`
- **YouTube Studio Checklist**:
  - Visibility: **Public** (or **Unlisted**)
  - Audience: **No, it's not made for kids**
  - Show More -> **Allow embedding**: **Checked ✅** (required so the player loads inside Devpost!)
  - Duration: Confirm total video length is **`<= 3:00`**
- **Video Description (Copy-Paste)**:

```text
AI Pipeline (agent-orchestrator) — Kiroween 2026 Hackathon Submission (Frankenstein Category).

AI Pipeline stitches cloud LLM APIs, local coding CLIs, hardened Docker sandboxes, real OS test commands, non-executing AST/JSON reviews, and hash-locked human approvals into a zero-dependency Python 3.11+ multi-agent workbench.

Try the 60-second zero-key judge verification:
python -X utf8 agent-orchestrator/scripts/verify_judge_demo.py

Links:
- GitHub Repository: https://github.com/zjungang462-netizen/ai-pipeline
- Judge Quickstart (START-HERE): https://github.com/zjungang462-netizen/ai-pipeline/blob/main/START-HERE.md
- Devpost Project: https://devpost.com/software/ai-pipeline

Highlights shown in this demo:
- Zero-key reproducible 5-stage workflow in the English Kiroween workbench
- Attempt #1 fails real Python assertions (exit code 1) and routes feedback back to coding
- Attempt #2 repairs the implementation in an isolated Git worktree and passes tests (exit code 0)
- Independent Python AST review writes review_verdict.json before hash-locked human approval
- Spec-driven architecture with /.kiro specs, steering rules, and PostFileSave verification hooks
```

---

## 4. GitHub Repository "About" Settings (`https://github.com/zjungang462-netizen/ai-pipeline`)

Click the ⚙️ gear icon next to **About** on the top-right of the GitHub repo page:

- **Description**:
  `Zero-dependency Python multi-agent orchestrator stitching cloud LLM APIs, local coding CLIs, Docker sandboxes, and real OS test gates into an evidence-locked workbench (Kiroween 2026 · Frankenstein).`
- **Website**:
  `https://devpost.com/software/ai-pipeline`
- **Topics**:
  `kiroween` `frankenstein` `kiro` `multi-agent` `ai-agents` `orchestrator` `llm` `python` `developer-tools` `code-review`

---

## 5. Screenshots, Git Push & Verified Judge ZIP

### Step A — Upload the 4 Pre-Generated Clean English Screenshots to Devpost (`Already Saved!`)

All 4 clean `1024x640` English (`?lang=en`) **`🎃 Kiroween`** screenshots have **already been generated in an isolated workspace and saved under `docs/screenshots/`**:
1. `docs/screenshots/aipipeline_01_home.png` — Clean English home screen (`0` old tasks, `10 adapters`, `⚡ Load the five-stage demo template`).
2. `docs/screenshots/aipipeline_02_test_failure.png` — `Awaiting approval` (`4 / 5 stages`) scrolled to `Test runner` (`Exit code: 1`), with clean English formatting.
3. `docs/screenshots/aipipeline_03_repair_diff.png` — `Code diff · Implementation` (`SHA verified`, `- return value` -> `+ return '-'.join(value.lower().split())`).
4. `docs/screenshots/aipipeline_04_review_approval.png` — `Completed` (`5 / 5 stages completed`), AST `Verdict: PASS`, hash-verified approval applied, and `review_verdict.json` visible in `Artifact archive`.

Replace the 2 old Chinese screenshots on your Devpost project gallery with these 4 PNG files using the captions in **Section 1**.

*(Optional — only if you ever want to launch a clean isolated demo workbench yourself:)*

```powershell
$repo = (Resolve-Path .).Path
$cleanDemo = Join-Path $env:TEMP ('ai-pipeline-clean-demo-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $cleanDemo | Out-Null
Copy-Item (Join-Path $repo 'agent-orchestrator\kiroween-demo.example.toml') "$cleanDemo\kiroween-demo.example.toml"

Push-Location (Join-Path $repo 'agent-orchestrator')
try {
    python -X utf8 -m orchestrator.cli --lang en dashboard --database "$cleanDemo\workbench.sqlite3" --config "$cleanDemo\kiroween-demo.example.toml" --port 8776 --open
} finally {
    Pop-Location
}
```

### Step B — Commit & Push All Fixes (Turns GitHub Actions CI Green ✅)

```powershell
$repo = (Resolve-Path .).Path
Set-Location $repo
python -X utf8 agent-orchestrator/scripts/precommit.py --checks-only --base-ref HEAD
git status --short
# Replace the placeholder with only the files you reviewed for this submission.
git add -- path/to/reviewed-file
git commit -m "fix(release,workbench,docs): harden cross-platform gates, i18n, and Kiroween judge package"
git push origin main
```

### Step C — Build the Dual-Verified Judge Release ZIP & Upload to GitHub Releases

*(Run after `git commit` so the working tree is clean:)*

```powershell
$repo = (Resolve-Path .).Path
Set-Location $repo
$releaseOut = Join-Path $env:TEMP ('kiroween-release-' + [guid]::NewGuid().ToString('N'))
python -X utf8 agent-orchestrator/scripts/release.py --output $releaseOut
```

Then open `https://github.com/zjungang462-netizen/ai-pipeline/releases/new`, select tag `v0.5.10` (create it if needed), attach the generated `.zip` archive from `$releaseOut`, and click **Publish release**.
