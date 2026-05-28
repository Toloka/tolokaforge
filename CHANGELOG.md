# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - 2026-05-28

First public release of Tolokaforge — a benchmarking harness for evaluating tool-using LLM agents.

### Highlights

- **Docker-based sandboxed execution** — all tool calls proxy into containerised services with no external network access
- **Multi-provider support** — any provider supported by LiteLLM (OpenAI, Anthropic, Google, Azure, Bedrock, Ollama, OpenRouter, and more)
- **MCP-compatible tooling** — tasks declare tools via Model Context Protocol or built-ins
- **Deterministic grading** — JSONPath assertions, state hashes, transcript rules, optional LLM judges
- **Rich metrics** — pass@k, cost/token estimates, latency percentiles, failure attribution

### Features

1. **LLM Layer** (`tolokaforge/core/llm/`) — Protocol-driven policy modules:
   - `reasoning.py` / `reasoning_codec.py` — structured thinking-block extraction + replay
   - `schema_sanitizer.py` — `ToolSchemaSanitizer` with RE2 post-condition
   - `cache_policy.py` — `CachePolicy` with ephemeral-cache implementation
   - `usage.py` — `Usage` dataclass + `UsageExtractor` + field-wise `__add__`
   - `params_policy.py` / `content_policy.py` / `response_policy.py` — provider-specific policies
   - `capabilities.py` — `ModelCapabilities` with all seven policy slots
   - `presets.py` — preset registry with reverse-lookup + fingerprint helpers

2. **Task Packs** — `evaluation.task_packs` support across Docker runtime with multi-root mock-web routing via `TASKS_DIRS`

3. **Artifact Writer** — `tolokaforge/core/output/artifacts.py` with `TrialArtifactWriter` Protocol + `FileArtifactWriter`

4. **Public Examples** — reference task layouts across all benchmark types:
   - `examples/native/coding/` — file-write grading
   - `examples/native/tool_use/` — structured tool-call grading
   - `examples/native/browser_task/` — browser tool against mock-web fixtures
   - `examples/native/native_shared_domain/` — `_shared/domain.yaml` + FastMCP pattern
   - `examples/terminal_bench/` — Docker-compose stacks with `terminal_bench` adapter

5. **Tiered CI Pipeline** — PR smoke tests, nightly/full tests, release gate

### Provider Support

- **GPT-5.x** — `StrictSchema` strips RE2-incompatible patterns + collapses Pydantic `Decimal` idiom
- **Qwen** — `qwen` preset with `schema_sanitizer: strict` + `response_policy: array_dict_map` + `prompt_policy: dict_map_hints`
- **Claude 4.x** — `anthropic_claude_4_7` preset with canonical `thinking={}` kwarg + automatic `cache_control` markers
- **Gemini** — full support via LiteLLM

### Documentation

- [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) — LLM abstraction layer reference
- [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md) — contributor guide for adding new models/providers
- [`docs/TASKS.md`](docs/TASKS.md) — task authoring guide
- [`docs/GRADING.md`](docs/GRADING.md) — grading system reference
- [`docs/TOOLS.md`](docs/TOOLS.md) — tool reference
- [`docs/RUNNER.md`](docs/RUNNER.md) — runner & distributed execution

### Requirements

- Python 3.10+
- Docker (required for all tool execution)
- `litellm>=1.83.14,<2.0.0`
