# Tasks: Unified Adapter Layer

- [x] 1. Abstract `Adapter` contract — `adapters/base.py` _(R1)_
- [x] 2. Declarative `AdapterSpec` + `validate_options` — `adapters/spec.py`; tests: `test_adapter_spec.py` _(R2)_
- [x] 3. Factory from config type — `adapters/factory.py` _(R2)_
- [x] 4. OpenAI-compatible adapter with SSE and env-only keys — `adapters/openai_compat.py`; tests: `test_api_adapter.py` _(R3)_
- [x] 5. CLI base: env allow-list, timeout, version probe — `adapters/cli_base.py` _(R4)_
- [x] 6. Per-tool CLI adapters (Claude Code, Codex, Qwen, Qoder, Doubao, Grok, Antigravity, DSH) — `adapters/cli_*.py`; tests: `test_cli_adapters.py` _(R4)_
- [x] 7. Verified runtime manifest and Docker isolation helpers — `adapters/verified_runtimes.py`, `adapters/docker_base.py` _(R5)_
- [x] 8. `FakeAdapter` for offline showcase — `adapters/fake.py`
