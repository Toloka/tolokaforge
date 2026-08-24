# Tolokaforge Test Suite

## Overview

The test suite is organized into **3 categories**: unit, canonical, and integration.

| Category | Directory | Speed | External deps | Marker |
|----------|-----------|-------|---------------|--------|
| Unit | `tests/unit/`, `tolokaforge_models/tests/unit/`, `tolokaforge_coding_harnesses/tests/unit/` | Fast (< 1s each) | None | `@pytest.mark.unit` |
| Canonical | `tests/canonical/` | Fast (< 5s each), except the packaging tests that build a wheel from the current tree — the subset partition audit builds one per module, and the packaging/entry-point smoke tests build one per session and install it into a scratch venv (~10–25s) | Golden snapshots; the packaging/entry-point smoke tests also require the `uv` CLI (they skip loud without it) | `@pytest.mark.canonical` |
| Integration | `tests/integration/`, `tolokaforge_models/tests/integration/` | Slow (5-60s each) | Docker, API keys | `@pytest.mark.integration` |

Each workspace package that owns a contract keeps its own test root beside its source, so
a future repo split takes the tests with the code. Passing a path to pytest overrides
`[tool.pytest.ini_options] testpaths`, so a command naming only `tests/` runs a subset and
still reports green — name all three roots, or pass no path at all.

Current baseline: run the lane you care about (`mcp__dev__run_tests marker=unit`,
`marker=canonical`) — the counts move with every merge, so they are not written down
here.

## Running Tests

Unit and canonical tests run without API keys or Docker. Omit the path and pytest reads
`testpaths` from `pyproject.toml`, which names all three roots:

```bash
# All non-integration tests
uv run pytest -v -m "not integration"

# Unit tests only
uv run pytest -v -m unit

# Canonical tests only
uv run pytest -v -m canonical

# Regenerate golden snapshots
uv run pytest tests/canonical/ --update-canon -v
```

Integration tests need `.env` variables (API keys, service URLs) — use `scripts/with_env.sh`. They run in parallel with `-n auto` (pytest-xdist):

```bash
# Integration tests (needs Docker + API keys in .env)
scripts/with_env.sh uv run pytest -v -m integration -n auto

# Full suite
scripts/with_env.sh uv run pytest -v
```

Under `-n auto`, `tests/integration/reset_recipes/conftest.py` assigns each xdist worker a unique `COMPOSE_PROJECT_NAME` for the reset-recipe suite, whose stacks all share the `compose` basename and would otherwise collide across workers. The rest of the integration suite derives per-test project names from slug-encoded `make_project_temp_dir` basenames, so it stays in disjoint namespaces without the env pin.

### Regenerating the well-formed judge payload fixture

`tests/unit/grading/data/wellformed_submit_report.json` is a real judge's
well-formed `submit_report` payload (markers matching their verdicts). The unit
test `tests/unit/grading/test_rubric.py::TestWellFormedLivePayload` re-validates
it with no spend. To recapture it from a live judge run (real provider, small
spend), set `TF_CAPTURE_JUDGE_PAYLOAD=1` — the mid-tier (gpt-4.1-mini) case of
the marker acceptance test writes the fixture:

```bash
TF_CAPTURE_JUDGE_PAYLOAD=1 scripts/with_env.sh uv run pytest \
  tests/integration/test_rubric_judge_live.py::test_rubric_judge_live_markers_match_verdicts \
  -m integration
```

The rejection fixtures alongside it (`ae_bdg_*_submit_report.json`) are **frozen
historical captures** — see `tests/unit/grading/data/README.md`; never
regenerate those.

### Live rubric-judge retry recovery

`tests/integration/test_rubric_judge_live.py::test_rubric_judge_live_recovers_through_forced_retry`
drives a real OpenAI-family judge through the `submit_report` retry path. The
model's tool call is real on both turns; the test forces exactly one validation
rejection (by patching `parse_submit_report` as bound in the judge module) so the
retry fires deterministically, then asserts the live provider accepts the
repaired tool-call/tool-result sequence (COMPLETED, no 400). Key-gated on
`OPENROUTER_API_KEY` / `OPENAI_API_KEY`; skips without a key.

### Live user-simulator no-restart regression

`tests/integration/test_user_simulator_live.py` sends one real user-model call
(Claude Sonnet via OpenRouter; key-gated on `OPENROUTER_API_KEY`, skips without
it) reproducing the seeded-opening restart shape: a backstory quoting the exact
opening line plus a transcript where the agent has already answered it. The
deterministic context-shape lock lives in
`tests/unit/test_user_simulator_context.py`; this live test exists because the
failure mode is a *model behaviour* triggered by context shape — the unit test
pins what the model is shown, this one pins that a real model shown it neither
re-sends the opening nor re-introduces the customer. Costs one Sonnet call per
integration run.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures, auto-skip requires_api hook
├── unit/                    # Pure-logic tests, no I/O
│   ├── grading/             # Grading subsystem tests
│   └── adapters/            # Adapter unit tests
├── canonical/               # Golden snapshot + packaging/discovery smoke tests
│   ├── conftest.py          # --update-canon flag, canon_snapshot + built_wheel session fixtures
│   ├── test_adapter_convert_packaging.py    # wheel-inspection packaging smoke
│   ├── test_adapter_convert_entry_point.py  # entry-point discovery smoke (scratch-venv install)
│   ├── fixtures/            # tolokaforge-adapter-demo: demo conversion adapter installed into a scratch venv
│   └── snapshots/           # Committed golden JSON files
├── integration/             # Docker/API integration tests
│   ├── coding_harnesses/    # tolokaforge_coding_harnesses against real containers
│   ├── deploy/              # Deployment-path tests
│   ├── docker/              # Docker foundation layer tests
│   ├── llm/                 # Live provider probes outside the certification suite
│   ├── network_policy/      # Egress enforcement against a live daemon
│   └── reset_recipes/       # Reset-recipe stacks (own conftest for xdist project names)
├── data/                    # Test data
│   ├── tasks/               # Task fixtures (calc_basic, browser_basic, calc_custom_checks)
│   ├── grading_parity/      # Substrate-parity packs; own glob, outside tasks/**
│   ├── transcript_parity/   # transcript_rules differential packs; own glob, may author two keys
│   ├── projects/            # Full project snapshots (food_delivery_2, tau_retail_mini)
│   ├── grading_bundles/     # Authored grading bundles the verdict pins read; each README says it was not recorded
│   ├── curation_runs/       # Recorded runs curate builds a corpus from; one record-carrying, one not
│   ├── migration_corpora/   # Judge-labelled trial bundles reconcile reads; each half curate wrote carries its corpus.yaml
│   ├── migration_packs/     # Migration declarations reconcile resolves; shipped task_ids, never a default root
│   └── configs/             # Config fixtures
└── utils/                   # Shared test utilities
    ├── fixtures.py           # Common fixtures (mock_env_state, test_task_path, etc.)
    ├── validators.py         # Output validation helpers
    ├── doc_anchors.py        # GitHub-style anchor + section extraction for the canonical doc locks
    ├── wheel_builds.py       # Subset / models wheels built from the tree under test, into a caller-supplied dir
    ├── mock_clients.py       # MockAsyncClient — canonical source
    ├── networks.py           # Docker network/volume fixtures
    ├── containers.py         # Docker container fixtures
    ├── docker_helpers.py     # Compose/daemon helpers for the Docker tiers
    ├── orchestrator_stubs.py  # GradingCompleteness a stub Orchestrator publishes — the CLI reads the attribute with no default
    ├── recorded_calls.py     # RecordedToolCall builders
    ├── runner_requests.py    # gRPC request + TaskDescription builders
    ├── servicer_runtime.py   # RuntimeBackend over the in-process servicer + the duplicate-call_id refusal
    ├── conductor_phases.py   # Arguments for the conductor's _grade / _write_artifacts phases
    ├── search_plane_harness.py  # RegisterTrial through the search plane: registry stand-in, kb task, both address sources
    ├── timelines.py          # Coherent TrialTimeline fixtures (message view + records)
    ├── grading_parity_packs.py  # A grading_parity pack's trial.yaml, decoded once into each substrate's input shape — the canonical differential and the docker gate suite read one set of bytes
    ├── trace_constraints.py  # One trace constraint evaluated, for single-verdict assertions
    ├── trace_checks_configs.py  # One authored trace_checks block spanning the whole vocabulary
    ├── trace_overrides.py    # A supplied constraint block, written to a file and loaded back
    ├── five_shape_run.py     # One run dir holding every trial shape the harness writes; corpus for both offline commands
    ├── provision_failure.py  # The task-less bundle a provisioning failure leaves, written through the production executor path
    ├── migration_packs.py    # A task directory a migration declaration is read out of, and the corpus its entry names
    ├── combine_method_verdicts.py  # The combine.method answer table both tiers hold
    ├── golden_source_shapes.py  # Non-list golden_actions shapes every reading surface must refuse
    ├── wire_grades.py        # A wire Grade driven through the real gRPC client lowering
    ├── fourth_wall_corpus.py  # The fourth-wall detector's pass/detect rows, read by two suites
    └── project_fixtures.py   # food_delivery_2 project data loaders
```

## Test Categories

### Unit Tests (`tests/unit/`)

Pure logic tests with no external dependencies. Mock everything.

- Grading checks: hash computation, JSONPath assertions, transcript rules
- Grading key ledger (`grading/test_grading_ledger.py`) — a failure means either a
  populated scored `grading.yaml` key reaches `GradeTrial` with no evaluator and no
  recorded skip, or the ledger rejects a config that grades correctly
- Tool registry: schema conversion, tool invocation
- Adapter output: task bundle generation, conversion logic
- CLI commands: status, validation paths

### Canonical Tests (`tests/canonical/`)

Compare output against committed golden snapshots in `snapshots/`.

- Adapter conversion output
- Grading pipeline results
- Custom checks with real project data (food_delivery_2)
- Golden-set hash grading verification
- Authoring-gate corpus (`test_example_pack_grading_corpus.py`) — every pack under
  `examples/` and `tests/data/tasks/` faces the whole gate against its own tool
  inventory and effective combine, and
  `test_the_packs_outside_the_gate_walk_are_held_to_the_whole_gate` holds the 49
  authored packs outside those roots — `grading_parity`, `transcript_parity`,
  `tests/data/projects` and `tests/data/migration_packs` — to the same gate. A new
  parity fixture whose grading names a tool its `task.yaml` never declares fails
  there, before anyone runs `validate`. Every pack in that walk carries both of the
  walk's positive controls — a tool no actor can call and a weight naming no
  component — so the sweep cannot read clean by having stopped running.
- Grading substrate parity (`test_grading_substrate_parity.py`) — a failure means a
  `grading.yaml` key is unaccounted for, claims a substrate that does not evaluate
  it, addresses a position below a claimed field by something other than an element
  path — the manifest's one mechanism for that — no longer survives adapter
  translation, names a `runner_field` the runtime ledger cannot resolve, stopped
  folding a listed numeric-string field by name on one of the two substrates, or an
  unmakeable binding comparison stopped failing the candidate it was read on — or
  its sentence stopped crossing the wire. For that last one, fix the reduction in
  `tolokaforge/core/grading/trace_checks.py`; for the rest, fix the manifest entry in
  `tolokaforge/core/grading/key_manifest.py` or the drift it exposed; widening a
  frozen exemption set in the test module is the deliberate last resort.
  A **lock 15** failure is narrower: one ledger key's recording site was deleted,
  downgraded to a skip, filed `EVALUATED` for an evaluation that did not run, or its
  driver stopped populating the key. The sweep covers **every** ledger key naming a
  runner field — the hash family, the db probes and the judge included, not a subset
  — so the failing row names the key. Fix the evaluate-or-skip site in
  `_grade_trial_async` (or the evaluator it calls) rather than the assertion. A key
  missing from the lock's driver table fails
  `test_every_ledger_key_names_a_driver_that_can_populate_it` instead, which means a
  new ledger key arrived with no way to drive it: add a driver, never drop the row.
  **Locks 16-20 are not about the manifest at all** — they are about what a grade
  *does* and *says*, which the manifest does not describe, so neither the manifest
  entry nor an exemption set is ever the repair. A **lock 16** failure means the two
  substrates disagreed on `state_checks.hash.expect_initial_state` — the proposition
  "the trial left the state as it found it", each substrate hashing both sides in its
  own algebra: fix whichever evaluator moved. A **lock 17** failure means the
  substrates' `Custom checks:` segment diverged, or the shared renderer stopped
  naming the check that decided the trial: fix `custom_checks_reason` in
  `tolokaforge/core/grading/checks_helpers.py`, or whichever call site re-wrapped
  what it returned. A **lock 18** failure means a registered grade component
  contributes no segment to `Grade.reasons`: fix the branch in `build_grade_reasons`
  that names it — **never** the marker in `_COMPONENT_NARRATION`, which is the
  written-out counterpart the lock exists to hold the renderer against. A component
  added to `GRADE_COMPONENTS` with no row fails
  `test_every_registered_component_has_a_narration_row` instead: add the row and the
  branch together.
- Transcript rules substrate parity (`test_transcript_substrate_parity.py`) — one
  authored pack under `tests/data/transcript_parity/`, one trial, graded through both
  substrates' real adapter paths, must produce the same `transcript_rules` component
  and the value the row pins. Twenty-two rows: twenty sit on the nine scoring questions
  a transcript rule has to answer the same way on either substrate, and two unmarked
  **anchor** rows, at two different scores, sit on no question at all and are the
  harness's own proof. A row pinning a value the runner does not produce raises
  `_FixtureDefect` — that is the table being wrong, not the substrates disagreeing.
  Fix the divergence in `tolokaforge/core/grading/transcript.py`, never the pinned
  column. What an events-less trial scores is a property of the fold rather than of one
  pack's verdict, so the two named tests beside the table drive the core engine's
  `grade_trajectory` against the runner's own `GradeTrial` RPC instead.
- Trace timeline substrate parity (`test_trace_timeline_substrate_parity.py`) — one
  scripted tool-call sequence driven through each substrate's real recording path
  must build the same events. A failure means one substrate's recording drifted,
  so a trace check would mean different things depending on which substrate graded
  the trial. Both substrates then *grade* off that timeline, so a failure here also
  means both substrates' transcript rules are reading a trial the two views no
  longer agree on. One lock reads the ids as recorded, before any timeline: its
  failure means the loop stopped assigning episode-unique ids at ingestion
  (`ToolCallingLoop._assign_call_ids`), not that a substrate drifted — the
  runner half executes the ids the loop produced, as production does.
  Build a coherent message-view/record pair for a grading fixture
  with `tests/utils/timelines.py`; a record naming a call no message asked for is a
  reconciliation failure, not a shortcut. `build_timeline` lands every call on the
  last assistant turn, while `build_turn_timeline` takes the calls per turn — which
  is what an ordering or turn-window property needs.
- Schema version stamps documented (`test_schema_version_stamps_documented.py`) — the
  stamps `docs/OUTPUT_FORMAT.md` § Schema Version Stamps publishes, both the table's
  rows and the bare `schema_version: N` literals the prose repeats, must equal the
  constants that write them. Every other stamp test compares a stamp against its own
  constant, so a bump nobody documented reds nothing; the table is the second source. A
  failure means the constants moved and the docs table follows — never the other way
  round.
- Simulator prompt generation (`test_simulator_prompt_generation.py`) — the sha256 of
  the rendered LLM user-simulator prompt body, per generation of
  `Trajectory.simulator_schema_version`. A failure means either the prompt body moved
  without the stamp bump that dates it, or the stamp was bumped without a manifest row
  for the generation it opened. Fix by reconciling the two in one commit: the stamp,
  the row, and the body. `--update-canon` does not touch this module — the manifest is
  hand-edited by design, because a regenerated snapshot would let the very edit the
  module exists to catch be blessed by the commit that made it.
- Protocol version documented (`test_protocol_version_documented.py`) — the versions
  `docs/GRPC_PROTOCOL.md` § Version lock names, one sentence each, must be exactly
  `1..ENGINE_PROTOCOL_VERSION`. Every in-tree caller reads the constant, so a bump
  nobody documented reds nothing while the doc keeps telling an operator that the
  newest version is the previous one; a sentence above the constant describes a gate
  nothing enforces. A failure means the section follows the constant — document what
  the new version is the first to change, never edit the constant to match the prose.
- Gate semantics parity (`test_gate_semantics_parity.py`) — the judge's required
  criterion and a trace check's `severity: gate` are one gate scored by two
  implementations, driven against one shared answer table. A failure names the cell
  where they disagree. No shared helper can replace it: the trace fold's weighted
  fraction carries one division and no branch, while the judge's must raise on a
  non-positive denominator. The same file holds `docs/GRADING.md`'s two gate sections
  to cross-referencing each other, so neither spelling can be documented alone.
- Grading wire census (`test_grading_wire_lock.py`) — the key paths the trial spec puts
  on the wire under `task.grading` and `task.search`, written out by hand and compared
  against an independent walk of the declared runner models: the path, the gate its
  emission waits on, and the shape its value crosses as. A field added to a runner
  grading model without a census row fails naming the path; so does a census row naming
  a key no model declares, a gate the models contradict, a shape that widened, and a
  retired path a model re-declares. The walk's stops (`_WALK_STOPS`) and the census's
  declared leaf containers are two hand-written constants held equal — neither is
  derived from the other, because a walk that stopped wherever the census declared a
  leaf would let one hand edit delete a subtree from both sides at once. Every
  pack under `examples/native/**` is then built and serialised the way the conductor
  serialises a trial spec, so "the model declares it" and "the adapter emits it" are
  separate claims. Add the row; never widen the walk to match the census. Every censused
  key must additionally carry a version lock or be named in the exemption set that
  records it as predating the support floor — so a grading field added below any
  container, not only a new top-level key, has to join the version-lock table or be
  argued into that set. The set is a snapshot, which is what makes the check decidable
  without a release per row: a key in neither was added after it.
  The same file holds `docs/GRADING.md` § Runner-engine version lock — the subset that
  locks an engine to a runner image — to the census: same keys, same directions, a
  breadth column rendered from the census's *measured* gate rather than written by
  hand, a release per key that `CHANGELOG.md` records, and no key dated below the
  support floor the table's own preamble states. `docs/RUNNER.md` and
  `docs/TROUBLESHOOTING.md` are held to resolving pointers at that heading, so renaming
  it reds rather than returning the reader to nothing. The table is read by two
  independent readers held to the same rows — `doc_anchors.section` bounds a body at any
  `^#{1,3} ` line without tracking code fences, so a `#` comment inside a fenced block
  ends its read early and every lock iterating the rows would pass over the ones it never
  saw (#986). Never put a fenced block inside that section.

Every substrate-parity pack lives in `tests/data/grading_parity/<task_id>/` and
authors a `task.yaml` and a `grading.yaml`, and — where its grading or its trials
name a tool — an `mcp_server.py` with the `fixtures/tools.json` described below. A
pack that drives a differential adds a `trial.yaml` holding one named case per
trial it grades — conventionally `satisfying` and `violating` — in the one shape
its loader reads:

```yaml
satisfying:
  messages:
    - { role: user, content: "Refund PAY-1 if it is a duplicate." }
    - role: assistant
      content: "Looking that up."
      tool_calls:
        - tool_name: billing_api_get_payment
          executor: agent
          status: success
          arguments: { payment_id: "PAY-1" }
          output: '{"amount": 10}'
  state: {}
```

A tool call belongs to the message that requested it, so a case places its calls
across the turns that made them and the timeline's `turn_index` follows what the
author wrote. `output` is that call's own result text and defaults to `""`. Call
ids and `sequence` are assigned in document order, and `latency_seconds` is not
authorable — wall time is not compared across substrates, so a pinned value would
be one nothing reads. Any other key fails the load naming itself, because a
fixture key the loader ignores expresses less than its author wrote.

A pack whose key reads DB state declares its rows in `task.yaml`'s `initial_state`,
not only in the case's `state:` — the runner provisions a trial's DB service from
`initial_state`, and lock 15 grades every pack through `RegisterTrial`.

A parity pack declares every tool its grading block and its trials name, in the
block matching the actor the grading names: `tools.agent.enabled` for a rule about
the agent's calls, `tools.user.enabled` for one about the simulator's. The
authoring gate refuses a matcher, a `tool_expectations` entry or a
`required_actions[i].name` naming a tool no actor can call, and refuses a
`required_actions[i].requestor` whose own actor does not declare the tool — so a
pack that puts a `requestor: user` action's tool in the agent's block fails the
gate, and a declaration short of the timeline describes a trial the task could not
have run.

Where the tools are **builtins**, that is the whole of it: the schemas come from
the builtin registry, and the pack ships no `mcp_server.py` and no
`fixtures/tools.json` — `user_executed_action` declares `calculator` and carries
neither. An **MCP-backed** pack sources its schemas from its own `mcp_server.py`
through the `fixtures/tools.json` it commits beside it — a JSON list of
`{name, description, parameters}` covering *every* enabled tool, including
`write_file`, because one server sources all of an MCP-bearing task's tools (a task
naming a second server in its user block is refused at load). Each `parameters`
object declares as `properties`, under `additionalProperties: false`, every
argument name that pack's matchers address and its timeline sends, which is what
puts the gate's argument checks at error tier rather than leaving them unchecked.
Nothing starts these servers — both parity suites substitute a call's recorded
result before any `ExecuteTool` — so a script's bodies echo their arguments: the
script exists to define the tools whose schemas the pack ships.

The pack directory is the author key with its dots replaced by underscores, so a
leaf key inside a list field gets its own pack:
`trace_checks.constraints.absent_before` is
`tests/data/grading_parity/trace_checks_constraints_absent_before/`. Two packs are
named for what they are instead, because no author key names them: `all_keys`,
which declares the whole manifest at once, and `user_executed_action`, whose
subject is a `requestor: user` action being reachable at all rather than a key of
its own. A suite reaching a pack by key gets it by that rule; these two are named
by the suites that read them. A pack under
test for one constraint kind authors **that kind alone** at the top level — a
second constraint beside it is a second check the violating trial could be
discriminated by — while the sub-terms of a composite kind belong inside its own
expression, where they are part of the constraint under test rather than next to
it. Write the two trials so that a build ignoring the thing under test would score
them *identically*: that is what makes discrimination evidence for the key rather
than for the pack.

`tests/data/transcript_parity/` is the second parity root, read only by
`test_transcript_substrate_parity.py` through its own glob. Its packs author a
`task.yaml` and a `grading.yaml` and **no `trial.yaml`**: each trial's messages and
its `tool_log` live in the module's table beside the score the row pins, because
whether the trial kept a record of the calls it declared is one of the things the
two substrates read differently, and a fixture loader deriving records from the
message view could not express its absence. A pack here may author **two** author
keys — a veto is only distinguishable from a fraction with a second rule beside it —
which is why these packs cannot live under `grading_parity`, whose one-key rule is
what keeps a violating trial attributable to the key under test.

Use `--update-canon` flag to regenerate the snapshots under `snapshots/` after
intentional changes. The guard modules above that compare code against a doc or a
hand-edited manifest have no snapshot behind them, so the flag does not reach them —
they are reconciled by editing the file the failure names.

### Integration Tests (`tests/integration/`)

Require Docker daemon, API keys, or both. Auto-skipped when prerequisites are missing.

- Docker container lifecycle and service health
- End-to-end runner pipeline (tau, tlk_mcp_core, native)
- LLM-judged grading with real providers
- Security: container isolation, network segmentation

A test that exercises a runner-side wire field needs `make docker-build-core`
first, because the field ships inside the image. Only a *missing* image skips: a
**stale** `tolokaforge-runner:latest` fails at `RegisterTrial` with a pydantic
extra-forbid error naming the field (the runner config models are
`extra="forbid"`), which points at the test rather than at the image. The builder
tags by content hash and does **not** move `:latest`, which is the tag the
testcontainer fixtures pin, so retag after building:
`docker tag <fresh-ref> tolokaforge-runner:latest` (#740).

**No test run downloads the embedding model.** The rag-service image carries
`all-MiniLM-L6-v2` and runs offline, so the 88MB download happens once, when the
image is built or pulled — a test run costs a ~3.5s container start. The
build-only-when-absent rule bites here the same way it does for the runner: a
**stale** `tolokaforge-rag-service:latest` predating that bake is never rebuilt,
and it downloads the model at container start, inside the fixture's 60s wait. On
a fast link it succeeds and the suite merely runs slower; on a slow one the wait
expires mid-download, and the log tail the fixture attaches shows exactly that.
Rebuild and retag:

```bash
uv run tolokaforge docker build --service rag-service
docker tag <fresh-ref> tolokaforge-rag-service:latest
```

When a container fixture's wait strategy does give up, the failure names the
service and carries that container's last 40 log lines, so the reason is in the
pytest output rather than in a `docker logs` command nobody ran.

Some suites in this tier are the `enforcing_test` a grading-key manifest entry
names — `test_docker_grading_hash_composition.py` for the `state_checks.hash`
family and for `state_checks.numeric_string_fields`,
`test_helpdesk_workflow_end_to_end.py` for `state_checks.db_probes`,
`test_rubric_judge_live.py` for `llm_judge`. `test_grading_substrate_parity.py`
resolves each nodeid without importing it — every entry carrying one, whatever its
enforcement tier — so renaming one of those test functions fails the canonical tier
naming the manifest entry.

`test_docker_grading_trace_gate.py` drives the committed `trace_checks_gate`
parity pack — the one the canonical differential reads, through the one decoder in
`utils/grading_parity_packs.py` — through a containerised runner: `RegisterTrial`
with the pack as `NativeAdapter` resolves it, then `GradeTrial` over real gRPC with
the authored trajectory as `llm_messages_json`. What it locks that no canonical
test can is the **image**: the parity suite's runner half is an in-process servicer
built from the working tree, while the image installs the
`tolokaforge-runner-subset` wheel, whose file partition already excludes part of
`core/grading`. Off the wire it pins the verdict, the `trace_checks`
component, `trace_checks_summary.gate_failed`, `failed_gate_ids`, the per-constraint
`severity` values and the sentence naming the gate that tripped. It refuses to grade
against an image the current tree did not build: the container's image must carry
`expected_image_ref("runner")` among its tags, which is what the fixture's own
build-and-tag path leaves there, so a stale `:latest` fails naming
`uv run tolokaforge docker build --core` rather than grading under an older tree.
Keyless: no LLM, no API key, and no tool is executed.

`tests/integration/docker/test_terminal_bench_per_trial.py` is the end-to-end
lock for the terminal-bench per-trial substrate: it materialises the
`fix-billing-holds` staging directory, builds the engine images with the docker
CLI baked in and aliases them as `tolokaforge-{runner,db-service}:local`, runs
the adapter-declared `docker compose build` for the agent image, then drives
`PerTrialRuntimeBackend.provision` → `register_trial` → `execute_tool` (probing
`/tests/test.sh` + `/logs/{verifier,agent}` inside the container) → `grade_trial`
→ `teardown`. Prerequisites: a running Docker daemon and the
`examples/terminal_bench/fix-billing-holds` task pack. No LLM key required — the
test drives the substrate directly, not the CLI. `make docker-build-core` warms
the engine-image cache but is not strictly required: the fixture rebuilds them
with `enable_docker_cli=True` because the plain `docker-build-core` runner ships
without the docker CLI the bash tool needs. First run takes a few minutes for
the task's Dockerfile (PostgreSQL + FastAPI); subsequent runs hit the cache.

Two members of this tier are **Docker-free and keyless** (they need only the `uv`
CLI): `test_plugin_discovery.py` and `test_external_harness_e2e.py`. Both install
the out-of-tree `tests/fixtures/tolokaforge_plugin_fixture` package into an
isolated `--target` (never the dev venv) and drive it in fresh subprocesses.

`test_external_harness_e2e.py` is the runtime-independence capstone: a single
downstream plug-in registers a runtime backend, grader, and conductor, and both
`tolokaforge.runner.run_trial` and the `tolokaforge run-trial` subprocess resolve
all three seams from installed metadata.
`test_run_trial_cli_over_downstream_plugins` compares the subprocess's `result`
wire line to a checked-in golden at
`tests/data/run_trial_capstone_golden.jsonl`. The fixture trajectory carries no
volatile field (`messages=[]`, pinned `start_ts`/`end_ts`, default `Metrics`), so
the golden is byte-stable with **no mask**. Regenerate it after an intentional
wire/fixture change with:

```bash
TOLOKAFORGE_REGENERATE_GOLDEN=1 scripts/with_env.sh uv run pytest \
  tests/integration/test_external_harness_e2e.py::test_run_trial_cli_over_downstream_plugins
```

If two regenerations differ, a volatile field leaked — pin it in the fixture, do
not add a mask.

## Pytest Markers

Defined in `pyproject.toml` under `[tool.pytest.ini_options]`:

| Marker | Description |
|--------|-------------|
| `unit` | Fast, isolated unit tests |
| `integration` | Tests requiring external services |
| `canonical` | Canonization snapshot tests |
| `slow` | Tests taking > 5 seconds |
| `requires_api` | Needs LLM API key — auto-skipped if none set |
| `requires_docker` | Needs Docker daemon |
| `docker` | Real container tests |
| `requires_postgres` | Needs Postgres instance |
| `grading` | Grading system tests |
| `security` | Security validation tests |
| `performance` | Performance benchmarks |
| `llm` | Calls real LLM providers |

All markers are enforced via `--strict-markers`.

## Key Fixtures

| Fixture | Source | Used by |
|---------|--------|---------|
| `mock_env_state` | `utils/fixtures.py` | Unit tests for user tools |
| `test_task_path` | `utils/fixtures.py` | Integration Docker service tests |
| `temp_output_dir` | `utils/fixtures.py` | Integration Docker service tests |
| `canon_snapshot` | `canonical/conftest.py` | All canonical tests |
| `food_delivery_2_*` | `canonical/conftest.py` | Canonical golden-set tests |
| `json_db_container` | `utils/containers.py` | Integration security tests |
| `rag_service_container` | `utils/containers.py` | Integration tests needing `search_kb` |
| `runner_container` | `utils/containers.py` | Integration security tests |

## Writing New Tests

1. **Choose the right category**: unit for logic, canonical for regression snapshots, integration for real services.
2. **Naming**: `test_<component>_<behavior>.py` for files, `test_<action>` for methods.
3. **Use shared fixtures** from `conftest.py` — don't duplicate.
4. **Add markers**: every test file should use the appropriate `@pytest.mark.*`.
5. **Import `MockAsyncClient`** from `tests.utils.mock_clients` for new tests (don't create local copies).

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `food_delivery_2` tests skip | Run `git lfs pull` to fetch project data |
| Integration tests skip | Set API keys in `.env`, ensure Docker is running |
| `--strict-markers` error | Add new markers to `pyproject.toml` |
| Snapshot mismatch | Re-run with `--update-canon` if change is intentional — unless the failure names a doc or a hand-edited manifest, which the flag does not regenerate |

## Test Philosophy

- **No accepted failures**: every test passes, is deleted, or carries `xfail(strict=True, raises=…)` — a marker that records a measured defect and fails the suite the moment the defect is fixed, so the fix is what removes it. A bare or non-strict `xfail` absorbs real breakage silently and is never correct.
- **Zero bare `@skip`**: use conditional markers (`requires_api`, `requires_docker`).
- **Canonical golden data** for regression detection — diffs are reviewable in PRs.
- **Auto-skip** for missing prerequisites instead of hard failures.
