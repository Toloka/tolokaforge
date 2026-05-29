# Tolokaforge Architecture

This document is the entry point for the system-level architecture of Tolokaforge. It follows the [arc42](https://arc42.org/) section structure inlined as a single file, with [C4 model](https://c4model.com/) views drawn as Mermaid diagrams that render natively on GitHub.

For deep dives into individual subsystems, follow the links to `docs/*.md` from each section. For decision history and rationale, see [`adr/`](adr/).

> **How to evolve this doc:** keep the diagrams here at C4 Levels 1–2 (Context and Container) and treat them as a *reflection of what the code actually does today* — not what we plan to build. When a building block changes shape or a boundary moves, update the relevant section *and* add an ADR. When you want to propose a future state, add an ADR with status `Proposed`; once accepted, update the diagram in this file. Verify diagrams against the source (`tolokaforge/`) before merging changes here.

---

## 1. Introduction and Goals

Tolokaforge is a benchmarking harness for evaluating tool-using LLM agents. It runs multi-turn agent/user loops against task environments and produces deterministic pass/fail verdicts plus rich telemetry.

### Quality goals

| Goal | What it means in practice |
|---|---|
| **Determinism** | Same task + same model + same seed should yield the same verdict. State hashes, snapshot grading, transcript rules. |
| **Provider-agnostic** | Any LLM reachable via LiteLLM is a first-class citizen. Provider-specific quirks are isolated in per-preset capability policies. |
| **Isolation** | Tool calls can run in a separate process (and on a separate host) in Docker mode, with the agent loop never touching tool state directly. |
| **Honesty over convenience** | Failures surface explicitly. No silent fallbacks; no defaults that hide configuration mistakes. |
| **Extensibility** | New benchmark formats plug in as adapters without touching the core. |

### Primary stakeholders

- Model evaluators running benchmarks across providers.
- Task authors writing new benchmark task packs.
- Researchers analysing trajectories, failure modes, and cost/latency.

---

## 2. Constraints

### Technical

- **Language**: Python (managed via `uv`).
- **LLM access**: routed through [LiteLLM](https://github.com/BerriAI/litellm) — provider-specific glue is kept out of the core. Per-provider differences live in the LLM-layer preset registry (`tolokaforge/core/llm/presets.py`).
- **Tools**: in-process tool registry plus [Model Context Protocol](https://modelcontextprotocol.io) servers loaded from task packs.
- **Environment services** (optional, started by `make docker-up`): JSON state DB, mock web, RAG indexer — each one a separate container.
- **Trial-loop transport**: the agent–user loop runs **in-process** inside the orchestrator (`tolokaforge/core/runner.py:TrialRunner`). When the orchestrator runs in Docker mode, tool execution, environment state, and grading are delegated to a single **Runner gRPC service** (`tolokaforge/runner/service.py`).
- **Durable attempt queue**: SQLite for single-host runs, optional Postgres for multi-host workers (`tolokaforge/core/run_queue.py`).

### Organisational

- **Open source**: this repository is Apache-2.0; nothing committed here may reference internal-only systems by name.
- **Secrets**: only `SecretManager` reads credentials. Direct `os.environ` access for any secret is forbidden and CI-enforced. See [AGENTS.md § Secrets](../../AGENTS.md#secrets--single-abstraction).
- **Task packs are external**: benchmark task definitions live in separate repositories (public examples ship under `examples/`; production task packs are loaded via adapter glob from any directory the operator points at).

---

## 3. Context and Scope (C4 Level 1)

```mermaid
flowchart TB
    Evaluator["Evaluator / Researcher"]
    TaskAuthor["Task Author"]
    Forge["Tolokaforge<br/>(benchmarking harness)"]
    LiteLLM["LLM Providers via LiteLLM<br/>OpenAI · Anthropic · Google · Bedrock · OpenRouter · ..."]
    TaskPacks["Task Packs<br/>(external repos, glob-loaded)"]
    Docker["Docker Engine<br/>(optional: env services + Runner sandbox)"]
    Results["Result Artifacts<br/>(trajectories, metrics, schemas)"]
    Queue[("Attempt Queue<br/>(SQLite local / Postgres distributed)")]

    Evaluator -->|runs benchmarks| Forge
    TaskAuthor -->|authors| TaskPacks
    Forge -->|loads via adapter| TaskPacks
    Forge <-->|"tool execution, state, grading<br/>(Docker mode only)"| Docker
    Forge <-->|completions, tool schemas| LiteLLM
    Forge -->|writes| Results
    Forge <-->|durable attempts, leases| Queue
```

### Boundaries (what is *not* Tolokaforge)

- **LLM providers** — out of scope; accessed via LiteLLM.
- **Task content** — out of scope; lives in task pack repos. Tolokaforge defines the *contract* (task schema, tool interface, grading config), not the content.
- **Result analysis UI** — out of scope; result artifacts are stable YAML/JSON, consumed by downstream tooling (see [`tools/benchmark-analyzer`](../../tools/benchmark-analyzer/)).

---

## 4. Solution Strategy

| Decision | Rationale | Detail |
|---|---|---|
| **Adapter plugin architecture for benchmark formats** | Lets external task formats (`native` built-in, `terminal_bench` and other plugins) plug in without forking the core. | [`docs/ADAPTER_ARCHITECTURE.md`](../ADAPTER_ARCHITECTURE.md) |
| **In-process agent–user loop, Docker-delegated tool execution** | Keeping the loop in-process keeps the trial control flow debuggable; Docker delegation via the Runner gRPC service gives sandbox isolation when needed without forcing a service-mesh on local users. | [`docs/RUNNER.md`](../RUNNER.md) · [`docs/GRPC_PROTOCOL.md`](../GRPC_PROTOCOL.md) |
| **Provider-agnostic LLM layer with per-preset capability policies** | LiteLLM normalises envelopes, but providers diverge on schema dialects, reasoning encodings, caching, and tool naming. A preset registry keeps the quirks out of the orchestrator. | [`docs/LLM_LAYER.md`](../LLM_LAYER.md) |
| **Single secret abstraction** | One audited code path for every credential read; CI-enforced via static grep. | [AGENTS.md § Secrets](../../AGENTS.md#secrets--single-abstraction) |
| **Deterministic grading by default; LLM judges opt-in** | Deterministic verdicts are reproducible and cheap; judge calls are reserved for cases that genuinely need them. | [`docs/GRADING.md`](../GRADING.md) |
| **Durable attempt queue (SQLite / Postgres) with worker leases** | Lets long runs survive crashes and lets `prepare` + `worker` commands split a run across hosts. | `tolokaforge/core/run_queue.py` |

---

## 5. Building Block View (C4 Level 2 — Container)

```mermaid
flowchart LR
    subgraph Orchestrator["Orchestrator Process (single host)"]
        CLI["CLI<br/>(run · prepare · worker · validate · status · analyze)"]
        Core["Orchestrator + Core<br/>(orchestration · grading combine · metrics · search)"]
        TR["TrialRunner<br/>(in-process agent–user loop)"]
        Adapters["Adapter Layer<br/>(BaseAdapter · NativeAdapter · entry-point plugins)"]
        LLMLayer["LLM Layer<br/>(LiteLLM client · presets · schema sanitizers · reasoning codecs · cache policy)"]
        ToolsLocal["Tool Registry + Executor<br/>(built-ins + MCP, in-process)"]
        Secrets["SecretManager"]
        QueueLib["Run-queue client<br/>(SqliteRunQueue · PostgresRunQueue)"]
    end

    subgraph DockerOpt["Docker stack (Docker mode only)"]
        RunnerSvc["Runner Service<br/>(gRPC: RegisterTrial · ExecuteTool · GradeTrial · GetState · ResetTrial · CleanupTrial)"]
        DBSvc["db-service<br/>(JSON state DB)"]
        RAGSvc["rag-service<br/>(optional)"]
        MockWeb["mock-web<br/>(optional)"]
    end

    CLI --> Core
    Core --> Adapters
    Core --> TR
    Core --> QueueLib
    TR --> LLMLayer
    TR -- "local mode" --> ToolsLocal
    TR -- "Docker mode" --> RunnerSvc
    LLMLayer --> Secrets
    RunnerSvc <--> DBSvc
    RunnerSvc <--> RAGSvc
    ToolsLocal <--> DBSvc
    ToolsLocal <--> RAGSvc
    ToolsLocal <--> MockWeb
```

### Block responsibilities

| Block | Responsibility | Source | Detail |
|---|---|---|---|
| **CLI** | User-facing commands. `run` executes locally end-to-end; `prepare` + `worker` split a run for distributed execution. | `tolokaforge/cli` | — |
| **Orchestrator + Core** | Loads run config, instantiates the adapter, builds the task list, manages the attempt queue, runs trials, aggregates grades and metrics, writes artifacts. | `tolokaforge/core` (`orchestrator.py`, `grading/`, `metrics.py`, `search/`) | [`docs/RUNNER.md`](../RUNNER.md) |
| **TrialRunner** | One instance per trial. Owns the agent–user message loop, calls the LLM for both agent and user simulator turns, dispatches tool calls. | `tolokaforge/core/runner.py` | — |
| **Adapter Layer** | Resolves a benchmark format into `(TaskConfig, tools, grading, environment, docker_stack_requirements, task_description)`. Built-in `native` plus plugins discovered via the `tolokaforge.adapters` entry-point group. | `tolokaforge/adapters` + `external_adapters/` | [`docs/ADAPTER_ARCHITECTURE.md`](../ADAPTER_ARCHITECTURE.md) |
| **LLM Layer** | Provider-agnostic completion API on top of LiteLLM. Per-provider presets carry capability policies for schema dialect, reasoning encoding, parameter handling, caching, tool-name discipline. Used by both agent and user-simulator turns. | `tolokaforge/core/llm` | [`docs/LLM_LAYER.md`](../LLM_LAYER.md) |
| **Tool Registry + Executor (local)** | In-process tool registry and executor used in local (non-Docker) mode. Registers built-in tools and loads MCP servers declared by tasks. | `tolokaforge/tools` | [`docs/TOOLS.md`](../TOOLS.md) |
| **Runner Service (gRPC)** | Docker-mode delegate: owns per-trial state, reconstructs tools from a `TaskDescription`, executes tool calls, runs grading. Single service — not split into "agent" and "executor" services. | `tolokaforge/runner` | [`docs/RUNNER.md`](../RUNNER.md) · [`docs/GRPC_PROTOCOL.md`](../GRPC_PROTOCOL.md) |
| **db-service** | JSON state DB accessed over HTTP by tools (both local and Docker mode). Namespaced per `(task_id, trial_index)` for parallel isolation. | `tolokaforge/env/json_db_service` | — |
| **rag-service / mock-web** | Optional support services for RAG tasks and browser-style tasks. | `tolokaforge/env/rag_service`, `tolokaforge/env/mock_web_service` | [`docs/BROWSER_TOOLS.md`](../BROWSER_TOOLS.md) |
| **SecretManager** | Single read path for every credential (API keys, DB URLs, OAuth, signing keys). Subprocess export is the only sanctioned `os.environ` mutation, scoped narrowly. | `tolokaforge/secrets` | [AGENTS.md § Secrets](../../AGENTS.md#secrets--single-abstraction) |
| **Run-queue client** | Durable attempt queue. SQLite for single-host runs, Postgres for distributed worker pools. | `tolokaforge/core/run_queue.py` | — |

> **Note on the `tolokaforge/agent/` and `tolokaforge/executor/` packages.** These contain protobuf definitions and `*ServiceServicer` implementations for a planned multi-service decomposition, but they are not currently invoked by the orchestrator (`grep -rn "AgentServiceStub\|ExecutorServiceStub" tolokaforge/` returns no callers). Today only the single **Runner Service** is part of the live architecture. The dormant scaffolding is preserved while a decision about service decomposition is open — when that decision lands, an ADR should record the direction.

### Adapter plugin shape (zoom on the Adapter Layer)

```mermaid
flowchart TB
    Config["Run Config<br/>harness_adapter.type = native | terminal_bench | ..."]
    Disco["Entry-point discovery<br/>group: tolokaforge.adapters"]
    Base["BaseAdapter (abstract)"]
    Native["NativeAdapter<br/>(built-in · task.yaml)"]
    Ext1["External adapter A<br/>(separate package)"]
    Ext2["External adapter B<br/>(separate package)"]

    Config --> Disco --> Base
    Base --> Native
    Base --> Ext1
    Base --> Ext2
```

Every adapter implements the same `BaseAdapter` contract — including `get_task`, `create_environment`, `get_registry_tools`, `get_grading_config`, `grade`, `compute_golden_hash`, `to_task_description` (for Docker Runner registration), and `docker_stack_requirements` (to declare bind-mounts, Docker socket access, or DinD needs). The orchestrator only sees the contract, never the concrete adapter. New benchmark formats — public or private — plug in by publishing a Python package that registers under `[project.entry-points."tolokaforge.adapters"]`.

### Extension points (where external repos plug in)

Tolokaforge is the harness; the *content* (tasks, domain tools, grading data) lives in repositories outside this one. Four contracts let external repos extend the system without forking the core. Public adapter repos and internal/private task-and-tool repos use the same four contracts.

| Surface | Contract | Lives where |
|---|---|---|
| **Task packs** | A directory tree of `task.yaml` files (for the `native` adapter) or whatever format the adapter expects. Resolved via `evaluation.tasks_glob` and `evaluation.task_packs` in the run config. | Any directory the operator points at, in any repo. |
| **Domain tools (MCP servers)** | Each `task.yaml` may set `tools.agent.mcp_server: "mcp_server.py"` — a path *relative to the task directory* to a Python module that implements the task's tools (typically [FastMCP](https://github.com/modelcontextprotocol/python-sdk)). The harness imports it dynamically. Tools may also be declared as built-ins via `tools.agent.enabled`. | Alongside the task pack, or vendored from a separate tool library and referenced by import path. |
| **Custom adapters** | A separate Python package that subclasses `BaseAdapter` and registers under `[project.entry-points."tolokaforge.adapters"]`. The harness discovers it on `pip install`. | Independent Python package. `external_adapters/` shows the public pattern. |
| **Custom secret providers** | A subclass of `SecretProvider` registered with the `SecretManager` chain. Used to integrate Vault, AWS Secrets Manager, or other backends without changing call sites. | Anywhere in your Python environment; wired in at startup. |

These four contracts are the entire integration surface. Anything an external task-and-tool repo wants to do — supply new benchmark content, ship custom tools, register a new task format, swap credential backends — should fit through one of them. If something doesn't, that's a signal the contract needs an ADR-recorded extension.

---

## 6. Runtime View — One Trial

### Local mode (default)

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant CLI
    participant O as Orchestrator
    participant A as Adapter
    participant Q as RunQueue (SQLite)
    participant TR as TrialRunner (in-process)
    participant LLM as LiteLLM
    participant TE as Tool Executor (in-process)
    participant DB as db-service / rag-service (HTTP)
    participant G as GradingEngine

    U->>CLI: tolokaforge run --config run.yaml
    CLI->>O: load config + resolve task list
    O->>A: get_task · create_environment · get_registry_tools
    A-->>O: TaskConfig + tools + initial state
    O->>Q: enqueue (task_id, trial_index)
    Q-->>O: lease
    O->>TR: run trial
    loop until done, error, or max_turns
        TR->>LLM: agent.completion(messages, tool_schemas)
        LLM-->>TR: response (+ tool_calls)
        TR->>TE: execute(tool_call)
        TE<->>DB: state read/write
        TE-->>TR: tool_result
        TR->>LLM: user_simulator.reply(messages)
        LLM-->>TR: user turn
    end
    TR-->>O: trajectory + final_state
    O->>G: grade(trajectory, final_state, env)
    G-->>O: verdict + metrics
    O-->>CLI: write artifacts (trajectory.yaml, metrics.yaml, ...)
```

### Docker mode

The flow is identical except every interaction between TrialRunner and the tool/state/grading stack goes over gRPC to the Runner service:

```mermaid
sequenceDiagram
    autonumber
    participant TR as TrialRunner (in orchestrator process)
    participant LLM as LiteLLM
    participant RC as RunnerClient (gRPC)
    participant RS as Runner Service (gRPC, separate container)
    participant DB as db-service (HTTP)

    Note over TR,RS: Setup: orchestrator calls RegisterTrial(task_description)
    loop until done, error, or max_turns
        TR->>LLM: agent.completion(...)
        LLM-->>TR: response (+ tool_calls)
        TR->>RC: ExecuteTool(trial_id, tool_call)
        RC->>RS: ExecuteTool RPC
        RS<->>DB: state read/write
        RS-->>RC: tool_result
        RC-->>TR: tool_result
    end
    TR->>RC: GradeTrial(trial_id)
    RC->>RS: GradeTrial RPC
    RS-->>RC: verdict + metrics
```

The agent loop and the LLM calls always happen in the orchestrator process. Only tool execution, environment state, and grading move to the Runner service when Docker mode is active.

---

## 7. Deployment View

```mermaid
flowchart TB
    subgraph Local["Local single-host, in-process"]
        L_CLI["tolokaforge CLI"]
        L_Orch["Orchestrator process<br/>(TrialRunner + LLM client + in-process tools)"]
        L_SQLite[("SQLite<br/>(attempt queue)")]
        L_DB["docker-compose: db-service<br/>(+ rag-service, mock-web optional)"]
        L_CLI --> L_Orch
        L_Orch --> L_SQLite
        L_Orch <--> L_DB
    end

    subgraph Docker["Single-host Docker mode"]
        D_CLI["tolokaforge CLI"]
        D_Orch["Orchestrator process<br/>(TrialRunner + LLM client)"]
        D_Runner["runner container<br/>(Runner gRPC service)"]
        D_DB["db-service container"]
        D_Extras["optional:<br/>rag-service · mock-web · dind"]
        D_SQLite[("SQLite<br/>(attempt queue)")]
        D_CLI --> D_Orch
        D_Orch --> D_SQLite
        D_Orch <-->|gRPC| D_Runner
        D_Runner <--> D_DB
        D_Runner <--> D_Extras
    end

    subgraph Dist["Distributed (prepare + worker)"]
        D2_Prep["tolokaforge prepare<br/>(host: any)"]
        D2_PG[("Postgres<br/>(shared attempt queue)")]
        D2_W1["tolokaforge worker<br/>(host A)<br/>orchestrator + optional local Runner"]
        D2_W2["tolokaforge worker<br/>(host B)<br/>orchestrator + optional local Runner"]
        D2_Prep --> D2_PG
        D2_W1 <--> D2_PG
        D2_W2 <--> D2_PG
    end
```

The same orchestrator binary drives all three modes. Workers in distributed mode each contain a full orchestrator process and can independently run in local or Docker mode. Coordination happens entirely through the shared attempt queue — there is no central scheduler service. See [`docs/RUNNER.md`](../RUNNER.md) for the distributed contract.

---

## 8. Cross-cutting Concepts

| Concern | Where to look |
|---|---|
| **Secrets handling** | [AGENTS.md § Secrets — single abstraction](../../AGENTS.md#secrets--single-abstraction) · `tolokaforge/secrets` |
| **Determinism & state hashing** | [`docs/GRADING.md`](../GRADING.md) · [`docs/GOLDEN_TRIALS.md`](../GOLDEN_TRIALS.md) |
| **Per-provider capability handling** | [`docs/LLM_LAYER.md`](../LLM_LAYER.md) · [AGENTS.md § Known Gotchas](../../AGENTS.md#known-gotchas) |
| **Tool isolation & sandboxing** | [`docs/SECURITY.md`](../SECURITY.md) · [`docs/BROWSER_TOOLS.md`](../BROWSER_TOOLS.md) |
| **Telemetry & metrics** | [`docs/LOGGING.md`](../LOGGING.md) · [`docs/ANALYTICS.md`](../ANALYTICS.md) |
| **Result artifact layout** | [`docs/OUTPUT_FORMAT.md`](../OUTPUT_FORMAT.md) |
| **Configuration reference** | [`docs/CONFIG.md`](../CONFIG.md) · [`docs/REFERENCE.md`](../REFERENCE.md) |

---

## 9. Architecture Decisions

All architecturally significant decisions are recorded as ADRs in [`adr/`](adr/). The index is maintained by appending new ADRs in numerical order — never renumber, never delete. When an ADR is superseded, change its status and link forward to the superseding ADR.

See [`adr/README.md`](adr/README.md) for the process and [`adr/0000-template.md`](adr/0000-template.md) for the template.

---

## 10. Risks and Known Limitations

| Area | Note |
|---|---|
| **Provider drift** | Tool-schema dialects and reasoning encodings change without notice. Mitigated by capability tests per preset (see [`docs/LLM_LAYER.md`](../LLM_LAYER.md)) and explicit `ModelCertificate` declarations. |
| **Multi-turn vs single-turn behaviour gap** | Some model failures only surface in long-context multi-turn runs and pass synthetic single-turn probes. Tracked in [AGENTS.md § Known Gotchas](../../AGENTS.md#known-gotchas) #22. |
| **Dormant gRPC scaffolding** | `tolokaforge/agent/` and `tolokaforge/executor/` packages contain gRPC service implementations that nothing currently calls. Whether to wire them up or remove them is an open decision; an ADR should land before the next significant change to the Runner boundary. See also [`docs/FUTURE_DEVELOPMENT.md`](../FUTURE_DEVELOPMENT.md). |
| **Container resource accounting** | Sandboxed Runner shares the host; resource accounting is per-process, not cgroup-bounded. |

---

## Glossary

| Term | Meaning |
|---|---|
| **Adapter** | Plugin that maps a benchmark format to the harness contract (`BaseAdapter`). |
| **Task pack** | A directory tree of tasks resolved by an adapter (`task.yaml` for native; format varies per adapter). |
| **Trial** | One attempt at one task with one model configuration. A run produces `N_tasks × repeats` trials. |
| **Trajectory** | The full message + tool-call log for one trial, written as `trajectory.yaml`. |
| **TrialRunner** | The in-process Python class (`tolokaforge/core/runner.py`) that owns one trial's agent–user loop. |
| **Runner Service** | The gRPC service (`tolokaforge/runner/service.py`) that the orchestrator delegates to in Docker mode for tool execution, state, and grading. Distinct from `TrialRunner`. |
| **Preset** | Per-provider capability bundle (schema sanitizer, reasoning codec, params policy, cache policy, tool-content policy). |
| **Grading config** | Declarative spec of how a trajectory and final state are turned into a pass/fail verdict. |
| **Attempt queue** | Durable record of pending and in-flight trial attempts (SQLite single-host or Postgres distributed); leased by workers. |
