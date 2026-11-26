---
inclusion: always
---

# AI Pipeline Engineering & Kiro Steering Rules

1. **Zero Runtime Dependencies**: Keep `agent-orchestrator/orchestrator/` strictly dependent on Python 3.11+ standard library modules (`http.server`, `sqlite3`, `subprocess`, `json`, `urllib`, `pathlib`, etc.). Do not introduce third-party runtime packages.
2. **Evidence Over Self-Report**: Never mark a stage complete based solely on LLM text output. Require artifact diffs, exit codes, or structured JSON verdicts (`review_verdict.json`).
3. **Adapter Isolation & Pre-Flight Checks**: All CLI and API integrations must implement the adapter specification in `orchestrator/adapters/spec.py` and validate runtime compatibility before model invocation.
4. **Security & Repository Hygiene**: Never commit credentials, `.env` files, local SQLite databases, or runtime workspaces (`*.toml` local overrides, `_workers/`, `*-ws-*/`). Keep `/.kiro` tracked in version control.
