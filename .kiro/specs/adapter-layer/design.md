# Design: Unified Adapter Layer

## Modules
| Module | Responsibility |
|---|---|
| `adapters/base.py` (`Adapter`) | Abstract contract: `preflight`, `run` |
| `adapters/spec.py` | `OptionSpec`, `Capabilities`, `AdapterSpec`, `validate_options`, `render_adapter_tables` |
| `adapters/factory.py` | `build_adapter`, `build_all` from config `type` |
| `adapters/openai_compat.py` | HTTP + SSE (`parse_sse_stream`), key from env, writes verdict / code files |
| `adapters/cli_base.py` (`CLIAdapter`) | `run_cli_process`, env allow-list, timeout, version probe, verified preflight |
| `adapters/cli_*.py` | Per-tool argument building and output parsing |
| `adapters/verified_runtimes.py` | `VerifiedRuntime` manifest; `get_verified_runtime` |
| `adapters/docker_base.py` | `base_run_args`, `bind_mount`, `validate_image_ref`, `proxy_env_args`, `cleanup_container` |
| `adapters/fake.py` | Deterministic adapter for the zero-key showcase and tests |

## Contract
```
Adapter.preflight(ctx) -> PreflightReport
Adapter.run(stage: StageSpec, ctx: RunContext) -> Result
```

## Credential handling
- Config stores only the *name* of the key variable (`api_key_env`).
- CLI subprocess env is built from an allow-list; secrets are not inherited.

## Design decisions
- One spec drives validation, docs generation and UI, so they cannot drift.
- `FakeAdapter` lets judges run the full pipeline without any keys.
