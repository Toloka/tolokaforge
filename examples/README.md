# Examples

Reference task layouts for the adapters that ship with TolokaForge, organised
by adapter type.

    examples/
    ├── native/                          # default `native` adapter
    │   ├── browser_task/                # browser tool + mock-web fixtures
    │   ├── coding/                      # file-write grading
    │   ├── mock_web_booking/            # http_request against the mock-web service
    │   ├── native_shared_domain/        # _shared/domain.yaml pattern (FastMCP)
    │   ├── rag_search/                  # search_kb against the rag-service
    │   └── tool_use/                    # structured tool-call grading
    └── terminal_bench/                  # `terminal_bench` adapter (Docker compose)
        ├── fix-billing-holds/
        └── fix-airline-segmentation/

## Choosing an example

- **Most tasks**: start from `native/coding/` — the simplest pattern.
- **Tool-heavy tasks**: see `native/tool_use/` or `native/native_shared_domain/`
  (the latter shows the `_shared/domain.yaml` + FastMCP `mcp_server.py` pattern
  for sharing tools across test-cases).
- **HTTP against a service**: see `native/mock_web_booking/` — `http_request`
  drives the mock-web service and grading locks a mock-web-issued token (needs
  Docker for mock-web).
- **Knowledge-base retrieval**: see `native/rag_search/` — `search_kb` retrieves
  a planted fact from a per-trial rag-service index and grading locks that
  retrieval-only token (needs Docker with the full stack for the rag-service).
- **Browser tasks**: see `native/browser_task/` (needs Docker for mock-web).
- **Terminal-bench tasks** (Docker compose + `tests/test.sh`): see
  `terminal_bench/`.

## Running an example

Most examples ship a run config under `run_configs/`:

    uv run tolokaforge run --config examples/native/coding/run_configs/dev.yaml

Requires an LLM provider key in `.env` (or use `--provider mock` for offline
testing). Browser and terminal-bench examples also need a running Docker daemon.

See each example's `README.md` for full prerequisites.
