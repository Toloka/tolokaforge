# Examples

Reference task layouts for the adapters that ship with TolokaForge, organised
by adapter type.

    examples/
    ├── native/                          # default `native` adapter
    │   ├── browser_task/                # browser tool + mock-web fixtures
    │   ├── coding/                      # file-write grading
    │   ├── native_shared_domain/        # _shared/domain.yaml pattern (FastMCP)
    │   └── tool_use/                    # structured tool-call grading
    └── terminal_bench/                  # `terminal_bench` adapter (Docker compose)
        ├── fix-billing-holds/
        └── fix-airline-segmentation/

## Choosing an example

- **Most tasks**: start from `native/coding/` — the simplest pattern.
- **Tool-heavy tasks**: see `native/tool_use/` or `native/native_shared_domain/`
  (the latter shows the `_shared/domain.yaml` + FastMCP `mcp_server.py` pattern
  for sharing tools across test-cases).
- **Browser tasks**: see `native/browser_task/` (needs Docker for mock-web).
- **Terminal-bench tasks** (Docker compose + `tests/test.sh`): see
  `terminal_bench/`.

## Running an example

Most examples ship a run config under `run_configs/`:

    uv run tolokaforge run --config examples/native/coding/run_configs/dev.yaml

Requires an LLM provider key in `.env` (or use `--provider mock` for offline
testing). Browser and terminal-bench examples also need a running Docker daemon.

See each example's `README.md` for full prerequisites.
