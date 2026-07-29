# RAG search — `search_kb` against a first-party service

A single-task native pack that drives the first-party **rag-service** over the
`search_kb` builtin. The agent retrieves an operations fact from a per-trial
knowledge-base index and reports it; grading is deterministic and
keyless-gradable in shape — it asserts a knowledge-base search happened and that
a retrieval-only fact appears in the transcript.

```
agent → search_kb → rag-service  (/trials/{id}/search over the per-trial index)
```

## What this example demonstrates

- **`search_kb` against a composed peer, no `environment_manifest`.** The task
  enables `search_kb` and declares its corpus via `initial_state.rag.corpus_dir`
  (`rag/corpus`). The corpus's `.md` files travel with the task in
  `tool_artifacts` and are indexed into the rag-service per trial; the
  rag-service is reached by Docker DNS on the full standalone stack
  (`deploy/standalone/docker-compose.yaml`), the same way `mock_web_booking`
  reaches mock-web — the runner's own network wires the peer, not a per-task
  manifest.
- **A retrieval-only fact.** The Halden substation's emergency failover
  authorization code, `HX49-QORVEN-7731`, lives in exactly one corpus document
  and on no agent-visible surface — not the task text, not any initial state.
  The only way the agent can obtain it is a real `search_kb` retrieval, so a
  guessed or hallucinated answer cannot pass.
- **Deterministic grading on the retrieval outcome.** Grading is
  transcript-only:

  | Check | What it asserts |
  |---|---|
  | `required_actions` (`search_kb`) | the agent searched the knowledge base — the rag-service round-trip actually happened |
  | `must_contain: HX49-QORVEN-7731` | the retrieved authorization code is reported |

  Both are product-scored under `transcript_rules` with `pass_threshold: 1.0`,
  so dropping either — a no-search trajectory, or a missing code — fails the
  task.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/rag_search/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/rag_search/run_configs/dev.yaml
```

Needs a running Docker daemon with the standalone stack up on the full profile
(so the rag-service is reachable on the runner network) and `OPENROUTER_API_KEY`
in `.env`.

## Layout

```
examples/native/rag_search/
├── run_configs/dev.yaml          # haiku agent + user, no judge
├── project.yaml                  # discovery glob + native defaults
├── README.md                     # this file
└── dataset/tasks/kb_lookup_01/
    ├── task.yaml                 # search_kb-only, corpus via initial_state.rag
    ├── grading.yaml              # transcript-only: search_kb gate + retrieval-fact token
    └── rag/corpus/               # tiny KB corpus; the planted fact is in one doc
```

## Related

- [`docs/GRADING.md`](../../../docs/GRADING.md) — grading families, including
  `transcript_rules.required_actions` and `must_contain`
- [`docs/TASKS.md`](../../../docs/TASKS.md) — `initial_state.rag.corpus_dir` and
  per-trial RAG indexing
- [`docs/STANDALONE_RUNNER.md`](../../../docs/STANDALONE_RUNNER.md) — the
  composed standalone stack this pack runs against
