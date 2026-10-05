# 0054. The `SearchBackend` Protocol and entry-point registry

- **Status:** Accepted (changes 1 and 2 of 3 implemented)
- **Date:** 2026-09-30
- **Deciders:** @CiroGamboa, @rsmtnn
- **Supersedes:** none
- **Realizes:** [ADR-0011](0011-seam-and-declaration-conventions.md) § Pattern A

## Context and Problem Statement

A task that declares `initial_state.rag.corpus_dir` and `search_kb` in
`tools.<actor>.enabled` gets one retrieval implementation: the hybrid
rag-service (`env/rag_service/app.py`, BM25 + dense, per-trial index). Around it
the engine wires retrieval by the tool's name in two places, and ties a few
more to that name.

- **Agent-side binding.** `adapters/native.py` substitutes
  `create_search_kb_schema()` (`runner/tool_factory.py`: the name `search_kb`;
  `query`, `top_k` and `alpha` exposed to the agent) for an enabled
  `search_kb`. At `RegisterTrial`, `ToolFactory._create_wrapper` looks the name
  up in the builtin registry (`tools/builtin/registry.py`), where `search_kb` is
  `Dispatch.RAG`. That builds a `RAGSearchToolWrapper`, which calls the
  rag-service client and renders its JSON.
- **Judge-side binding.** `_resolve_judge_kb_search` (`runner/service.py`) gives
  the judge a `RagServiceKnowledgeSearch` only when an agent tool is an
  `isinstance(…, RAGSearchToolWrapper)`. The judge tool (`judge_tools.py`) cuts
  every hit to 200 characters, for any backend.
- **The name elsewhere.**
  - The tool inventory (`adapters/_task_loader.py`) and the "corpus ⇒ tool
    enabled" check (`native.py`) name `search_kb` literally.
  - `search_kb` in `_FULL_STACK_TOOL_NAMES`, or a truthy `initial_state.rag`,
    selects `full_stack` (`core/orchestrator.py`), which stands up rag-service
    with its embedding model.
- **`SearchPlane`** (`runner/models.py`, the `SearchConfig.plane` field of the
  wire's `TaskDescription.search`) has two values.
  - Only the native adapter sets it, and only `TYPESENSE` is branched on: the
    runner registers the mcp_core TypeSense client for the trial. The index is
    built host-side, and `orchestrator.typesense` in the run config selects it.
  - rag-service indexing is gated by `search.enabled`, so `RAG_SERVICE` is in
    effect a label.
- **`KnowledgeSearch`** (`core/grading/kb_search.py`) is a Protocol with three
  implementations: rag-service, the gRPC `KBSearch` form the remote grader uses,
  and the offline replay form. TypeSense has none; its judge gets a passthrough
  by name.

A second retrieval shape is now real: a benchmark whose knowledge-base search is
plain Okapi BM25 over the pack's documents, with:

- a fixed top-k, ties broken by corpus order, and no zero-score filtering;
- document ids and titles taken from the documents;
- only `query` exposed to the agent.

The hybrid cannot be configured into that. It filters out zero scores, sorts
unstably, returns no ids or titles, and shows the agent `alpha` and `top_k`. A
differently named tool cannot be declared at all: a source-less schema whose
name is not in the builtin registry is refused. Today that benchmark runs its
search inside its own MCP host, out of the engine's sight, so the engine's judge
and native task format never see the corpus.

Every comparable decision in the engine is a registry: runtime backends,
conductors, turn policies, agent loops, user simulators, grader and judge kinds.
Retrieval is the exception.

## Decision Drivers

- **The idiom is settled.** ADR-0050/0051 fixed the shape: a Protocol, a frozen
  Context, a `Callable[[Context], Impl]` factory, and an entry-point group
  resolved through `discover_entry_points`, plus an in-memory fixture and a
  conformance kit.
- **No third abstraction.** After this change there are two:
  - `SearchBackend`: which retrieval a task uses, keyed by the value
    `search.plane` already carries;
  - `KnowledgeSearch`: what a backend's index hands the judge.

  `SearchPlane` stops being a concept of its own, and no new value such as
  `IN_PROCESS` is added.
- **Bind by declaration, not by name.** Both binding sites resolve the task's
  backend. Neither tests for a tool name or a concrete wrapper class.
- **The default must not move — verified.**
  - A task that declares a corpus and `search_kb` today keeps rag-service, the
    same agent schema, the same JSON and the same judge snippet.
  - `initial_state.rag` becomes a typed model whose dump carries only the keys
    the author wrote, through a wrap serializer over `model_fields_set`. So
    `TaskConfig.model_dump()` of every shipped pack is unchanged.
  - The backend name travels in the existing `search.plane`, and
    `backend_config` is emitted only when it is non-empty. So the
    `TaskDescription` of every existing task is byte-identical: the runner and
    grader wire, the grading bundle, and the canonical `task_description.json`
    snapshot.
  - Checked with a prototype against all 155 engine task configs and 29 native
    `TaskDescription`s (see § Snapshots).
- **The built-in is not special-cased.** rag-service resolves through the
  registry like any third-party backend.
- **Benchmark-shaped configuration stays out of the engine schema.** Tokenizer,
  BM25 constants, an output template and an empty-query rule belong to the
  backend. They travel in an opaque mapping the engine never reads.
- **A backend runs in the runner.** The agent's tool executes in the runner
  process, so the loader is runner-reachable. The runner image installs only the
  subset wheel, so a backend the engine does not ship cannot reach the runner
  today, and the second implementation is built in.

## Considered Options

1. **The seam alone:** a `SearchBackend` Protocol, a `tolokaforge.search_backends`
   group, the backend named in `initial_state.rag.backend`, an opaque
   `backend_config`, and rag-service as the registered built-in.
2. **Option 1 plus a built-in in-process `bm25` backend:** pure Python, no numpy,
   the index built in the runner at `RegisterTrial`, no rag-service container,
   core stack. **Chosen.**
3. **Keep adding knobs to rag-service** (`deterministic: true`,
   `expose_alpha: false`, `tool_name`). Rejected: every knob is a branch on the
   one shared component and a field on the engine schema.
4. **Leave search to the task's own MCP server.** Rejected as the answer: the
   judge and the native task format then never get the corpus.

For the plane, three shapes were weighed:

- **P1: the plane as a property the backend declares, beside a new `backend`
  field.** Rejected: two fields hold one fact and need a consistency validator,
  and a new enum value such as `IN_PROCESS` breaks old runners. This is the third
  abstraction.
- **P2: the registry keyed by what the plane expresses, with `plane` carrying the
  backend's name.** **Chosen.**
- **P3: rename `plane` to `backend`.** Rejected: it changes every task's dump
  and breaks old runner images.

## Decision

Adopt **option 2 with plane shape P2**, as separate, independently reviewable
changes:

1. the seam, with rag-service moved onto it and the name `typesense` reserved;
2. the built-in `bm25` backend, with the judge's snippet length
   (`judge_snippet_chars`) its tasks need;
3. TypeSense as a registered backend (see below).

### `SearchBackend` and `SearchIndex` Protocols

Declared in `tolokaforge/core/search/backend.py` (`core/search/` exists and
holds the TypeSense client):

```python
@runtime_checkable
class SearchIndex(Protocol):
    async def search(
        self, query: str, arguments: Mapping[str, Any], *, budget_s: float
    ) -> SearchOutcome: ...
    def knowledge_search(self) -> KnowledgeSearch | None: ...   # None: this backend gives the judge nothing

@runtime_checkable
class SearchBackend(Protocol):
    name: str
    stack_service: str | None                                    # "rag_service" | None
    def tool_parameters(self) -> Mapping[str, Any] | None: ...   # the agent tool's JSON-schema `parameters`; None: no agent tool
    async def build_index(self, corpus_dir: Path | None) -> SearchIndex: ...
```

- **Both trial calls are coroutines.** The runner awaits `build_index` on its
  event loop at `RegisterTrial`, and the agent's tool call awaits `search` on the
  same loop, so a backend over a network service needs no bridge of its own.
  `budget_s` is the tool's declared per-call budget: rag-service bounds its HTTP
  request by it, so the runner's backstop never fires first.
- **`tool_parameters()`** is the whole JSON-schema `parameters` object (type,
  properties, required) and must declare `query`: the runner hands that argument
  to `search` by name.

- **`SearchOutcome`** is a frozen dataclass with two fields:
  - `hits: tuple[SearchHit, ...]`: the existing backend-neutral `SearchHit`
    (an optional `title` comes with `bm25`, change 2, together with its field in
    the remote grader's proto);
  - `rendered: str`: the text the agent receives.

  The backend owns the rendering, because the agent-visible text is part of what
  a backend reproduces. The hits are what the judge, the remote grader's
  `KBSearch` and replay read.
- **`stack_service`** replaces the facts `SearchPlane` implied: which stack
  service a task needs, if any. The stack rule reads it (see below).
- **Errors propagate.** A failed search is never rendered as empty results.

### `SearchBackendContext` + registry

- **The context** is a frozen dataclass: the `backend_config` mapping, the
  tool's declared name and description, a logger, and what the runner knows at
  `RegisterTrial` — the trial id, the knowledge base's `domain_name`, and
  `stack_services`, the runner's handles on the declared stack services it
  reaches (see § Stack services; rag-service's is the runner's one long-lived
  client, shared with the judge's search).
- **A trial-less context.** The orchestrator side (the native adapter building
  the agent's schema, the stack rule) builds a context with no trial id and no
  stack-service handles, and reads `tool_parameters()` and `stack_service` off
  the constructed backend: both may depend on `backend_config`. A factory is
  therefore cheap and free of side effects, and `build_index` refuses a
  trial-less context.
- **Registration.** Backends register under **`tolokaforge.search_backends`** as
  `Callable[[SearchBackendContext], SearchBackend]`.
- **Loading.** `load_search_backend(name)` and `available_search_backends()` use
  the fail-loud `discover_entry_points` / `_load` machinery. An
  `UnknownImplementationError` names the known registrations.
- **Ownership.** `plugin_registry` owns the group constant, the loader and the
  listing, and re-exports the Protocols, as for `AgentLoop` and `UserSimulator`.
- **The runner subset.** The group joins `RUNNER_REACHABLE_ENTRY_POINT_GROUPS`,
  so `test_runner_subset_partition` locks it into the subset wheel, and
  `core/search` joins the subset partition (`typesense_server.py`, which only the
  orchestrator uses, stays out).

### Stack services

A backend that runs over a service of the run's stack reaches it through the
runner, and that boundary is a declared, versioned surface in
`tolokaforge/core/search/stack_services.py`, as the proto surfaces are for the
wire. It imports only the standard library, so the runner subset ships it with
the seam.

- **The declared services.** `DECLARED_STACK_SERVICES` maps each name a backend
  may put in `stack_service` to a `StackService`: the name, the
  runtime-checkable Protocol its handle satisfies, and how a runner reaches it.
  A name outside it is refused at `load_tasks`, naming the task, the backend and
  the declared names, and again at `RegisterTrial`.
- **The handle Protocols.** Each carries exactly the members a backend may use.
  `RagServiceHandle` is `base_url`, `timeout`, `index_documents` and `search`;
  the runner's `RAGServiceClient` satisfies it, and its other members
  (`delete_index`, `health_check`, `close`) are the runner's own.
- **The container.** `StackServices` is frozen, with one field per declared
  service, holding the runner's handle or `None`. It refuses at construction a
  handle that does not satisfy its Protocol. `stack_services.get(RAG_SERVICE)`
  returns the handle typed by its Protocol, and refuses a service the surface
  does not declare and one this runner does not reach.
- **The runner enforces the declaration.** `RegisterTrial` builds a trial's
  index only when the runner reaches the stack service the backend declares; it
  refuses the trial otherwise, naming the service and how a runner reaches it
  (for rag-service, `RAG_SERVICE_URL`, which the full stack sets).
- **Versioning.** `STACK_SERVICES_API_VERSION` numbers the surface: the declared
  names and every member of every handle Protocol. Every change to it — a service
  declared or withdrawn, a member added, changed or removed — increments the
  version and adds a row below. `tests/canonical/test_stack_services_contract.py`
  pins the surface to the version and checks the runner's client against each
  handle by signature, so a change without the bump fails CI. Adding a service or
  a member is compatible: a backend that needs it compares the version. Changing
  or removing a member breaks the backends that use it, so its row names what
  replaces it.

| Version | Surface |
|---|---|
| 1 | `rag_service`: `RagServiceHandle` — `base_url`, `timeout`, `index_documents(trial_id, domain_name, documents)`, `search(trial_id, query, limit=5, alpha=0.5, timeout=None)` |

### `search.plane` is the backend's name

- **The field.** `SearchConfig.plane` becomes `str | None`. It is checked
  against the registry at `load_tasks` and at `RegisterTrial`, not in the wire
  model, which the grader and bundle readers parse too.
- **The constants.** `SearchPlane` holds the constants of the built-in names
  (`RAG_SERVICE = "rag_service"`, `TYPESENSE = "typesense"`; `BM25` comes with
  change 2), as `AdapterType` does: canonical constants, not a closed set.
- **Existing tasks** serialize `"rag_service"`, `"typesense"` or `null`, exactly
  as today.
- **Old runner images** reject a task with `"bm25"`. That is honest: the feature
  needs the new image.
- **`search.enabled`** stays on the wire for version skew. The adapter derives
  it from the backend, and the runner's rag-service gate becomes
  `backend.build_index(...)`.

### The declaration is `initial_state.rag`, typed

```yaml
initial_state:
  rag:
    corpus_dir: kb/
    backend: rag_service        # default; the value search.plane carries
    backend_config: {}          # opaque, passed to the factory verbatim
    tool:
      name: search_kb           # default
      description: ...          # default: today's text
```

- **The model.** `initial_state.rag` becomes a typed model (it is `dict | None`
  today). A wrap serializer dumps only `model_fields_set`, so a task that writes
  `corpus_dir` alone dumps as `{corpus_dir: …}`, as now. Unknown keys are refused
  (`extra="forbid"`: every `rag` block in the engine and in the task repository
  declares `corpus_dir` alone), and because typing moves a malformed block's
  failure to load time, `load_tasks` refuses the run for one whatever
  `orchestrator.strict_task_load` says, rather than dropping the task.
- **The actor gets the tool as today.** Which actor gets it stays
  `tools.<actor>.enabled` naming the tool. The first draft's `tool.actors` is
  gone: it declared the same fact twice.
- **No project-wide default.** `task_defaults` has no `initial_state`, and the
  loader drops it without a word. So a project-wide default is not offered here;
  adding `initial_state` to `TaskDefaults` is a separate change.
- **The wire.** The adapter sets `search.plane = rag.backend`, and puts
  `backend_config` on `SearchConfig` only when it is non-empty. A tool name other
  than `search_kb` travels as `SearchConfig.tool_name`, also emitted only when it
  is not the default.
- **A corpus declares search on the wire.** In change 1 the adapter emits a
  `search` block only for a task that declares `corpus_dir`, as it does today. A
  task that enables the search tool with no corpus gets the tool's schema and no
  backend, and `RegisterTrial` refuses it when reconstructing the tool. A backend
  that serves a task with no corpus is a follow-up.
- **Refusal before any trial.** `Orchestrator.load_tasks` builds each searching
  task's backend from the trial-less context, as
  `_refuse_an_unregistered_user_simulator` resolves simulators: an unregistered
  name, or a backend refusing the task's `backend_config`, is one refusal naming
  the task and the backend.
- **The corpus.** `_bundle_corpus_artifacts` accepts `.json` documents next to
  `.md` and `.txt` — in change 2, since it changes the `tool_artifacts` of any
  existing pack with JSON files in its corpus directory.

### Re-homing the agent-side binding (`Dispatch.RAG`)

- **One construction site.** `RegisterTrial` resolves the backend from
  `task_description.search.plane` and builds the trial's `SearchIndex`; a backend
  may cache what it derives from the corpus (the runner does not: rag-service's
  indexes are per trial). It hands `ToolFactory` the pair
  `(index, tool_name)` in place of the rag-service client.
- **The wrapper.** `ToolFactory._create_wrapper` checks the declared search tool
  name before the builtin-registry branch, and returns a generic
  `SearchToolWrapper(schema, index)`.
- **What is removed.** `Dispatch.RAG`, the `search_kb` row of the builtin
  registry and `_create_rag_search_wrapper`.
- **rag-service's behaviour moves into its backend, byte for byte.**
  `RAGSearchToolWrapper` becomes the `rag_service` backend's
  `SearchIndex.search()`: its JSON rendering,
  `{"error": "Query is required", "results": []}` for an empty query, and the
  defaults `top_k=5`, `alpha=0.5`.
- **No new `InvocationStyle`.** The agent schema stays source-less, so
  `agent_tools[*].source` on the wire does not change for any task.
- **The schema.** `native.py` builds it from `tool.name`, `tool.description` and
  `backend.tool_parameters()`. For `rag_service` that is today's
  `create_search_kb_schema()`, byte for byte.
- **The literal name goes.** The inventory in `_task_loader.py` and the
  "corpus ⇒ tool enabled" check read the declared name.
- **The stack rule.** `search_kb` leaves `_FULL_STACK_TOOL_NAMES`, and
  `full_stack` is chosen by the backend's `stack_service`. Otherwise a `bm25`
  task with a tool named `search_kb` would still stand up rag-service. The rule
  is: a task that searches — declares a corpus or enables its search tool — and
  whose backend's `stack_service` is `rag_service` gets `full_stack`. A typed
  `rag: {}` is truthy as a model, and so does not count on its own, as the empty
  dict it replaces never did.

### Re-homing the judge-side binding (`isinstance(RAGSearchToolWrapper)`)

- **The judge's search.** `_resolve_judge_kb_search` returns
  `trial_index.knowledge_search()` when an agent tool is a `SearchToolWrapper`
  over the trial's index. The check is still by instance, not by name, so a
  renamed tool cannot fool it; it tests the generic wrapper.
- **rag-service keeps its search.** For `rag_service`, `knowledge_search()` is
  the same `RagServiceKnowledgeSearch(client, trial_id)` as today.
- **The snippet length (change 2).** `judge_snippet_chars` becomes a field of
  `JudgeCustomization`, next to `disable_knowledge_search`: default 200, `null`
  for full documents. It is passed to the judge's `SearchKbTool`. The default
  gives byte-identical output, and the wire does not change: `KBSearch` already
  returns full text. It lands with `bm25`, whose tasks need full documents;
  change 1 leaves the judge's 200-character cut as it is.
- **The judge's schema stays fixed.** It keeps `alpha`, which a backend without
  a hybrid weight ignores.

### TypeSense

`typesense` is registered as a built-in name in the same group. In the first
step its factory does what `_init_typesense_for_trial` does today:

- its backend declares `stack_service = "typesense"`;
- it gives no agent tool: the tool is the adapter's `search_policy`;
- `knowledge_search()` returns `None`, so the judge keeps today's passthrough.

The runner's plane dispatcher then goes through the registry, not an enum
branch. Until that change lands, the first change reserves the name: a
third-party backend registered as `typesense` is refused, and the runner keeps
its current TypeSense branch. There is one field and one registry either way. A
native `KnowledgeSearch` for TypeSense is a follow-up.

### The built-in `bm25` backend

`tolokaforge/core/search/bm25.py`: Okapi BM25 in pure Python, registered as
`bm25`, with `stack_service = None`. It follows `rank_bm25` 0.2.2's expression
order, so scores are bit-identical to the reference library without numpy.

Its `backend_config` is validated by the backend into its own `extra="forbid"`
model:

| key | default | meaning |
|---|---|---|
| `documents` | `{format: auto, order: filename, skip_prefix: "_", fields: [content]}` | JSON `{id, title, content}` or md/txt (id = file stem); loading order; which fields are indexed |
| `tokenizer` | `whitespace_lower` | a name from a small registry |
| `bm25` | `{k1: 1.5, b: 0.75, epsilon: 0.25}` | |
| `ranking` | `{top_k: 5, min_score: null, tie_break: corpus_order}` | fixed top-k, optional threshold, deterministic ties |
| `empty_query` | `no_results` | or `error` |
| `render` | `{kind: json}` | or `{kind: text, template: ..., score_format: ".4f", timing_suffix: off}` |
| `agent_parameters` | `[query]` | which of `query`, `top_k` the schema exposes |

#### As built (change 2a)

The backend landed as the table says, with these refinements, documented in
[CONFIG.md § `backend: bm25`](../CONFIG.md#backend-bm25--backend_config):

- **`render: {kind: text}`** has `item_template` (one hit over `{index}`, `{id}`,
  `{title}`, `{score}`, `{content}`, `{source}`), `separator`, `score_format`,
  `timing_suffix: off | measured` and `timing_template` (over `{retrieval_ms}`,
  `{reranking_ms}` — always `0`, there is no reranking stage — and `{total_ms}`),
  rather than one `template`; both renderers carry `empty_text` and `error_text`,
  the texts `empty_query` chooses between. Templates are validated at load:
  an unknown field is a refusal.
- **`documents.fields`** takes `title` and `content` only, and `documents.order`
  only `filename`: the one order the corpus has.
- **`ranking.min_score`** is inclusive (`score >= min_score`), as Elasticsearch
  reads it; `0.0` keeps zero scores.
- **A corpus BM25 cannot score is a refusal**, not an exception from the scorer: a
  document whose indexed fields are blank (a blank `title` under `fields: [title]`)
  names its file, and indexed text that tokenizes to no term at all is refused by
  `OkapiBm25`. On both, `rank_bm25` divides by zero; every other corpus scores bit
  for bit as the library does, including one where some documents are empty.
- **`SearchHit.source`** is the document's file name; the title has no field on
  `SearchHit` until change 2b adds it, and is read from the rendering.
- **The cache** is in the runner process, keyed by a digest of the corpus files'
  names and bytes and a digest of the config, sixteen most recently used.
- **`budget_s`** bounds nothing: the search is in process.
- **The judge** gets whole documents as `SearchHit.text`; the 200-character cut in
  `SearchKbTool` (`judge_snippet_chars`) is change 2b.
- `_bundle_corpus_artifacts` accepts `.json` next to `.md` / `.txt`. No shipped pack
  had a `.json` file under its corpus directory, so no `tool_artifacts` moved.

#### As built (change 2b)

- **`SearchHit.title`** is `str | None = None`. `bm25` fills it from the document;
  rag-service leaves it `None`. It crosses to the remote grader as
  `SubstrateSearchHit.title`, an `optional string` (tag 5), so an absent title
  reads back as `None`, never as an empty string. The judge's `search_kb` shows a
  `Title:` line only for a hit that has one, so a rag-service judge's output does
  not change.
- **`judge_snippet_chars`** is a field of `JudgeCustomization`: a strict positive
  integer or `null`, default `200`. It is not tri-state like its siblings: `null`
  means whole documents, so a task undoes a project figure by writing `200`. The
  dump leaves it out at its default, so every existing `TaskDescription` is
  byte-identical and an older image accepts a task that does not set it.
- **It reaches the judge in `JudgeTrialOptions`** (issue #1716), not as a keyword
  of its own. `JudgeKind.evaluate` takes the trial's per-trial customization as one
  frozen `options` object — `disable_knowledge_search`, `custom_system_prompt`,
  `include_agent_system_prompt`, `judge_snippet_chars` — so the Protocol's
  signature, the detachment surface for out-of-tree kinds, does not change when a
  knob is added: the knob is a field with a default. `resolve_judge_trial_options`
  builds it from `JudgeCustomization` in one place (`grade_llm_judge`, which the
  runner composite, the grader composite dispatch and the offline composite regrade
  call, and the `judge_only` helper, which lays the run-level override over it);
  replay builds it from the bundle, stamping each value's source in
  `replay_provenance.yaml`, `judge_snippet_chars` included.
- **Left out:** the run-level `grader.judge` overrides (`JudgeGraderConfig`),
  whose `None` means "inherit" and so cannot carry a `null` that means whole
  documents.

### Snapshots

We checked the claim that the default does not move with a prototype on
`f766b3e3`, comparing every engine task config and every native
`TaskDescription` before and after:

| Variant | `TaskConfig` dumps changed (of 155) | `TaskDescription`s changed (of 29) |
|---|---|---|
| `rag` as a plain typed model | 1 (`kb_lookup_01`: `backend`, `backend_config`, `tool` appear in `mode="json"`) | 0 |
| typed model + wrap serializer over `model_fields_set` | **0**, in every dump mode | **0** |
| control: a new defaulted field on `SearchConfig` | — | **29**, and `tbench_echo_hello/task_description.json` fails |

- **Why today's snapshots do not show it.** They stay the same under the plain
  model only because none of them has a `rag` block. The engine has one pack with
  `rag`, and it declares only `corpus_dir`.
- **The wrap serializer** is plain pydantic 2.x. `Field(exclude_if=…)` would need
  pydantic ≥ 2.11, and the engine allows `pydantic>=2.0.0`.
- **The tests** on the prototype: `-m "unit or canonical"` on the canonical
  suite and 33 related unit paths gives 3,232 passed in each variant. The one
  failure, `test_subset_venv_runner_boot_import_graph`, also fails on clean
  `f766b3e3` in the same environment.

## Consequences

### Positive

- A retrieval shape is a `pyproject.toml` row plus one class, not a branch in the
  tool factory and not a field on the engine schema.
- The judge searches the same index as the agent for every backend, by
  construction, and can read full documents where a task needs that.
- A deterministic BM25 task runs on the core stack: no rag-service container, no
  embedding model, no numpy in the runner.
- A task can name its search tool and choose what the agent sees.
- The plane, the stack rule and both bindings read one declaration.

### Negative / Trade-offs

- `backend_config` is opaque. A malformed value surfaces when the backend
  validates it at `RegisterTrial`, not at `config validate`. The refusal names
  the task and the backend.
- `SearchConfig.plane` loosens from an enum to a registry-checked string. The
  built-in names stay constants.
- The pure-Python BM25 is slower than numpy on large corpora. For a corpus of a
  few thousand documents an index builds in well under a second, and it is cached
  per corpus hash.
- Code that reads `initial_state.rag` as a dict (`.get("corpus_dir")`) breaks.
  In the engine there are three such readers, all changed. Task packages outside
  the engine may have their own.

### Follow-ups

- **A package-side backend.** The seam admits one, but the runner image cannot
  install it yet. Plugin delivery into the runner image is a separate decision.
- **`initial_state` in `TaskDefaults`**, so a project can set a pack-wide
  backend.
- **A native `KnowledgeSearch` for TypeSense.**
- **A backend with no corpus.** Change 1 declares a search backend on the wire
  only for a task with a corpus; a backend that serves a tool-only declaration
  needs the adapter to emit `search.plane` without `documents_path`.
- **rag-service hygiene** (optional): sorted document loading, stable ordering
  of equal scores, a switch for the zero-score filter, ids from file names, and a
  call to `delete_index` when a trial ends.

## Links

- Realizes: [ADR-0011](0011-seam-and-declaration-conventions.md) (Pattern A).
- Related ADRs:
  - [0050](0050-agent-loop-protocol-and-registry.md) and
    [0051](0051-user-simulator-protocol-and-registry.md): the same idiom;
  - [0020](0020-judge-protocol.md): the judge that consumes `KnowledgeSearch`;
  - [0025](0025-runner-wheel-split.md): the runner-reachable groups;
  - [0044](0044-composition-plan-runtime.md): stack selection.
- Related code:
  - `tolokaforge/core/grading/kb_search.py`, `tolokaforge/core/grading/judge_tools.py`;
  - `tolokaforge/runner/tool_factory.py`, `tolokaforge/tools/builtin/registry.py`;
  - `tolokaforge/runner/service.py` (`RegisterTrial`, `_resolve_judge_kb_search`,
    the plane dispatcher);
  - `tolokaforge/runner/models.py` (`SearchConfig`, `SearchPlane`,
    `JudgeCustomization`), `tolokaforge/runner/search_plane.py`;
  - `tolokaforge/adapters/native.py`, `tolokaforge/adapters/_task_loader.py`;
  - `tolokaforge/core/orchestrator.py`, `tolokaforge/core/plugin_registry.py`;
  - `tolokaforge/core/search/stack_services.py` (the declared stack-service surface);
  - `scripts/hatch/hatch_runner_subset_builder.py`.
- Docs:
  - `docs/RUNTIME_BACKENDS.md` § Plug-in extension points;
  - `docs/CONFIG.md` § `initial_state:`;
  - `docs/GRADING.md` § judge tools;
  - `docs/TASK_DESCRIPTION_SCHEMA.md`, `docs/TYPESENSE_INTEGRATION.md`,
    `docs/NATIVE_ADAPTER.md`.
