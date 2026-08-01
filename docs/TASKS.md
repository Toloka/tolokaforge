# Task Authoring Guide

This guide explains how to create and organize tasks in Tolokaforge.

## Task Directory

Task packs live outside the engine tree. Use the `task_packs` configuration to point at any directory containing tasks:
```yaml
evaluation:
  task_packs:
    - "/path/to/your/tasks"
  tasks_glob: "**/task.yaml"
```

## Task Layout

```
tasks/<category>/<task_id>/
├── task.yaml
├── grading.yaml
├── initial_state.json          # optional
├── www/                        # optional, for full-site browser tasks
├── mock_web/                   # optional, for single-page browser tasks
├── rag/corpus/                 # optional
└── README.md                   # optional
```

Categories: `terminal`, `browser`, `mobile`. Use `mobile` for app-style tasks that simulate phone interactions (restricted browser actions, no URL navigation). Use `browser` for full web browsing tasks. The mock-web service discovers static files from all categories automatically.

## task.yaml Essentials

```yaml
task_id: "shopping_review"
name: "Submit Product Review"
category: "browser"
description: "Submit a 4-star review on a mock website"

initial_state:
  json_db: "initial_state.json"
  mock_web:
    base_url: "http://mock-web:8080"
  filesystem:
    copy: []
  rag:
    corpus_dir: "rag/corpus"

tools:
  agent:
    enabled: ["browser", "db_query", "db_update"]
  user:
    enabled: []

user_simulator:
  mode: "llm"
  persona: "online shopper"
  backstory: |
    You bought a coffee maker and want to leave a 4-star review.
    When the agent confirms the review is submitted, say ###STOP###.

grading: "grading.yaml"
```

### Minimal task

The only required fields are `task_id` and `description`. Everything above is
optional and defaults to a sane value:

| Field | Default when omitted |
| --- | --- |
| `initial_state` | empty state (no JSON DB, filesystem, mock-web, or RAG) |
| `tools` | no tools enabled for agent or user |
| `user_simulator` | cooperative LLM user (`mode: llm`, `persona: cooperative`) |
| `grading` | a `grading.yaml` sitting next to `task.yaml` is picked up automatically; if there is none, the task has no grading configured |

So a task that inherits everything from its Project needs only:

```yaml
task_id: "api_endpoint_add"
description: "Add a POST /orders endpoint backed by the orders table."
```

## Initial State

- `json_db`: JSON file loaded into the JSON DB service. Use this for any task state that needs to be verified by grading.
- `filesystem.copy`: files copied into `/env/fs/agent-visible`.
- `mock_web.base_url`: base URL for mock web service (`http://mock-web:8080`).
- `rag.corpus_dir`: directory of knowledge-base documents for per-trial RAG
  indexing. The reader indexes the `.md` and `.txt` files sitting directly in
  that directory (flat, non-recursive). Declaring `corpus_dir` requires
  `search_kb` in `tools.agent.enabled` — the corpus files travel with the task,
  the runner indexes them into the rag-service per trial, and the agent's
  `search_kb` tool queries that index. The full stack must include the
  rag-service (reached by DNS, like `db-service`/`mock-web`). Declaring
  `corpus_dir` without `search_kb`, or pointing it at a directory that does not
  exist, is rejected at validation time.

## Multi-container environments (`environment_manifest`)

Optional. A task declares `environment_manifest` when it needs its own
docker-compose stack — extra services beyond the engine's built-in
`runner` + `db-service`. If omitted, the engine wires up its default
stack (extended to include mock-web / rag-service if the task uses their
tools).

The manifest points at a compose file that lives next to `task.yaml`, plus a per-service `services:` map that declares each service's isolation posture:

```yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
  services:
    app-db:
      isolation: "reset"
      reset:
        seed: "postgres_baseline"   # name from project-level assets.seeds
    # any service not listed here defaults to `ephemeral`
```

Field reference:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `compose_file` | yes | — | Path to the docker-compose YAML. Relative paths resolve against `task.yaml`. This file is the sole source of truth for services, images, ports, volumes, healthchecks, and `depends_on`. |
| `runner_service` | no | `"default"` | Which compose service is the tolokaforge runner. Must be a service declared in the compose file. |
| `services.<name>.isolation` | no | `"ephemeral"` | Per-service posture: `"shared"` (long-lived across trials), `"reset"` (fresh container per trial + `reset.seed` recipe reapplied at each provision), `"ephemeral"` (fresh container per trial, no seed). Backend selection is task-driven — any `reset`/`ephemeral` service routes the run to `PerTrialRuntimeBackend` automatically. See the [multi-container guide](MULTI_CONTAINER_GUIDE.md#choosing-isolation) for how to pick. |
| `services.<name>.reset.seed` | when `isolation: reset` | — | Name of the seed to apply on each provision. Must exist in the project's `assets.seeds` registry. See [`docs/RESET_RECIPES.md`](RESET_RECIPES.md) for the four seed kinds (`sql_dump` / `filesystem_dir` / `redis_dump` / `bare`). |
| `services.<name>.network_access` | no | `"default"` | Per-service opt-out from the harness-injected shared internal network. `"default"` (existing behaviour) attaches the service to `tolokaforge_netpolicy_internal` under `no_internet` and `limited_internet` and injects `HTTP(S)_PROXY` env under `limited_internet`. `"restricted"` skips both — the service joins only the networks its compose entry declares (which are still forced `internal: true`). Use for an untrusted sibling that must not reach other harness-injected services. See the [multi-container guide](MULTI_CONTAINER_GUIDE.md#partitioning-an-untrusted-sibling). |
| `network_policy` | no | `"no_internet"` | Public-egress posture for the task's application services. `no_internet` (default) attaches every task service (unless opted out via `services.<name>.network_access: restricted`) to an `internal` docker network so no service can reach the public internet; `full_internet` runs the compose file unchanged; `limited_internet` permits egress only to the hosts in `limited_internet_allowlist` via an injected forward proxy. See below. |
| `limited_internet_allowlist` | when `network_policy: limited_internet` | `[]` | Hosts application services may egress to under `limited_internet`. Each entry is a DNS hostname — exact (`api.openai.com`) or leading-wildcard subdomain (`*.openai.com`). Non-empty iff `network_policy` is `limited_internet` (validated at load). See below. |

`network_policy` enforcement (docker backends):

- `no_internet` (default) — every task service joins an injected `internal:
  true` network, so no application service reaches the public internet;
  inter-service DNS still works. The `runner_service` additionally joins a
  non-internal edge network so its published gRPC port stays host-reachable
  and it retains egress for in-container LLM-as-judge grading. The contract
  is scoped to application services — egress of tools the agent executes
  *inside* the runner is not blocked (#325). Individual services can opt out
  of the shared internal network via `services.<name>.network_access:
  restricted` — see above.
- `full_internet` — the compose file runs verbatim; every service keeps
  whatever egress its networks allow.
- `limited_internet` — application services join the injected `internal: true`
  network with no direct egress and are pointed at an injected digest-pinned
  `ubuntu/squid` forward-proxy sidecar (via `HTTP(S)_PROXY`). The proxy is
  default-deny and forwards only to the hosts in `limited_internet_allowlist`
  (bare hostname → exact match; `*.host` → subdomain suffix match); everything
  else is refused with HTTP 403. HTTPS goes through the proxy via CONNECT with
  no TLS interception, so pinned certificates keep working and no CA plumbing is
  needed. The `runner_service` joins the edge network directly (not proxied),
  keeping its grading egress, exactly as under `no_internet`. Entries are DNS
  hostnames only — schemes, ports, paths, IP literals, and duplicates are
  rejected at manifest load.

Fields declared on the model but **not yet enforced by the provisioner** —
declaring them is accepted for forward-compatibility but has no runtime
effect today:

- `security_context_defaults` — reserved. No provisioner consumer yet.
- `initial_state` (on the manifest itself, not the task-level
  `initial_state`) — reserved for per-service fixture copy operations. Use
  compose-file `volumes:` for now.

Compose-file safety invariants the manifest validator enforces at task
load:

- `network_mode: host` is rejected on any service.
- `privileged: true` is rejected on any service.
- `cap_add` is rejected on any service.
- Bind-mount paths must stay inside the task directory.
- All image tags must be pinned — `image: nginx:latest` is rejected;
  `image: nginx:1.27-alpine` is accepted.
- `depends_on` targets must reference declared services.
- `runner_service` must be declared in the compose file.

For a full walkthrough anchored to a working example, see the
[multi-container tasks guide](MULTI_CONTAINER_GUIDE.md). For the
underlying case matrix (built-in vs task-declared × shared vs per-trial)
see [ADR-0018](adr/0018-multi-container-under-shared-runtime.md).

## User Simulator

Prefer LLM mode (`mode: "llm"`) for realistic conversations. Use `backstory` to define the user's goal and information they reveal over the conversation:

```yaml
user_simulator:
  mode: "llm"
  persona: "impatient customer"
  backstory: |
    You need to reschedule your delivery to next Tuesday.
    Do not reveal all details at once — answer the agent's questions naturally.
    When the agent confirms the reschedule, say ###STOP###.
```

Scripted mode (`mode: "scripted"`) is available for simple deterministic flows but produces less realistic conversations.

### Specialised personas

For adversarial, multi-step tasks where the agent must *extract* the deciding
facts through conversation, sharpen the `backstory` into a specialised persona.
A specialised persona is a `backstory` with six components:

- **Named persona.** Give the user a name, role, and organisation ("You are Ana
  Reyes, operations coordinator at Northwind Biologics"). Concreteness keeps the
  simulator in character across a long exchange.
- **Facts the user knows.** State plainly what this person is aware of — the
  problem they are chasing, the constraints they live under — so the simulator
  answers consistently.
- **Reveal-on-ask rules.** List the deciding facts the user will confirm *only
  when asked*, each with an explicit "do NOT volunteer this unprompted". This
  forces the agent to investigate rather than be handed the answer.
- **Never name the solution.** Bar the user from naming the resolution, quoting
  policy, or otherwise doing the agent's reasoning ("Never name a resolution path
  or quote policy; that is the agent's job").
- **Natural opening.** Instruct the user to open in their own words with the
  problem, not a list of fields — this is what makes the first turn realistic.
- **`###STOP###` exit.** Give a precise exit condition ("Say `###STOP###` once
  the agent confirms a resolution is recorded"), not a generic "when done".

The worked example is
[`examples/native/multi_service_helpdesk_workflow`](../examples/native/multi_service_helpdesk_workflow/):
its coordinator persona knows the shipment is delayed and after-hours, confirms
the site has no cold storage or specialist only when asked, and never names the
policy-correct resolution — so the agent must reconcile four services plus a
policy corpus to derive it.

## Browser vs Mobile Tool

Use `browser` for full web browsing tasks (URL navigation, search). Use `mobile` for phone app tasks (no URL bar, mobile viewport).

```yaml
# Browser task - full web browsing
tools:
  agent:
    enabled: ["browser"]
    browser:
      initial_url: "http://mock-web:8080"   # Optional

# Mobile task - phone app interaction
tools:
  agent:
    enabled: ["mobile"]
    mobile:
      apps:
        DoorDash: "http://mock-web:8080"
      initial_app: "DoorDash"
```

The `mobile` tool uses a phone-sized viewport (412x915) and only exposes tap, type, scroll, and gesture actions — no URL navigation, search, or browser-specific actions. See [BROWSER_TOOLS.md](BROWSER_TOOLS.md) for full details.

## Mobile App Fixtures

Mobile app tasks in `tasks/mobile/` share a common mock data layer and theming approach:

- **Apps live in** `tasks/mobile/app_*` with static assets under `www/<domain>/`.
- **Brand variants** live in `brand/real.json` and `brand/fictional.json`. Set with `?brand=real` or `?brand=fictional`.
- **Shared dataset** lives in `tasks/mobile/_data/v1/` and is served by the mock-web API:
  - `GET /api/app-data?app=<app>&dataset=v1`
  - Files: `places.json`, `menus.json`, `hours.json`, `reviews.json`, `reservations.json`, `grocery_items.json`, `coffee_menu.json`, `events.json`, `notes.json`
  - Prefer deterministic values (e.g., `open_now`) instead of real-time clocks.
- **JSON DB conventions** for grading: `orders`, `grocery_orders`, `coffee_orders`, `reservations`, `searches`, `shortlists`, `calendar_events`, `notes`.

When authoring multi-app tasks, reuse the same `place_id` or item IDs across apps so the agent must reconcile information rather than guess.

## Mobile Benchmark Suite

The repository includes a 50-task mobile benchmark pack under `tasks/mobile/` (exclude `_templates` and the `app_*` fixtures). To run the full suite, point `tasks_glob` at the task folders:

```yaml
models:
  agent:
    provider: openrouter
    name: anthropic/claude-3.5-sonnet
    temperature: 0.0
  user:
    provider: openrouter
    name: anthropic/claude-3.5-sonnet
    temperature: 0.7

evaluation:
  tasks_glob: "tasks/mobile/*/task.yaml"
  output_dir: "results/mobile_benchmark"

orchestrator:
  repeats: 1
  max_turns: 25
```

To run a single task, change `tasks_glob` to its folder (e.g., `tasks/mobile/maps_opentable_calendar_sakura_dinner/task.yaml`).

## Browser Tasks

- Place HTML/JS/CSS in a `www/<sitename>/` subdirectory for full-site tasks, or `mock_web/` for single-page tasks.
- The mock web service serves files from `www/` subdirectories at `http://mock-web:8080/`.
- Use `initial_state.json` and JSON DB for any state that needs to be graded. HTML/JS should write to JSON DB, not to local files.
- Avoid external URLs — the environment network is sandboxed.

## Grading Tips

- Prefer `state_checks.jsonpaths` for deterministic, objective checks.
- Use `transcript_rules` to enforce tool usage patterns.
- Use `llm_judge` only for genuinely subjective evaluation (not as a softener for weak state checks).
- For RL training value, use strict grading: `state_checks` weight 1.0, no LLM judge padding — unless an idle agent already satisfies the state, in which case `transcript_rules` needs a weight of its own (below).

See `docs/REFERENCE.md` for full schemas.

### Refusal tasks and other do-nothing passes

A task whose expected final state **equals** its initial state — the agent is meant
to refuse, or to conclude that nothing needs changing — grades an idle agent as a
success. The unchanged state *is* the expected state, so the hash comparison and
every `state_checks.jsonpaths` assertion written against it hold; and
`transcript_rules.max_turns` bounds the turn counter from above only, so zero turns
is within any limit. Nothing in such a pack asks whether the agent did anything at
all.

Declare an activity floor, and weight the component that carries it:

```yaml
transcript_rules:
  min_assistant_turns: 1        # the agent must have produced at least one turn
  must_contain: ["cannot"]      # and must have said why it is refusing

combine:
  weights: { state_checks: 0.5, transcript_rules: 0.5 }
  pass_threshold: 0.8
```

The floor is a gate on the whole `transcript_rules` component rather than one more
sub-check inside it: unmet, the component is `0.0` whatever the other keys scored,
and `grade.reasons` carries `Assistant turn count 0 below min_assistant_turns of
1`. **The `combine.weights` entry is not optional** — core admits a scored
component only when `combine.weights` declares a weight for it, so a floor declared
in a pack that weights `state_checks` alone is evaluated and then dropped before it
can reach the final score.

A floor above `max_turns` admits no turn count at all. `tolokaforge validate`
rejects such a pack, naming both keys and both values, so an unsatisfiable window
is caught before the run is paid for.

The floor counts assistant **generations**, not answers — three tool-call-only
turns with no prose satisfy `min_assistant_turns: 3`, so pair it with a phrase rule
when the refusal itself is the deliverable. See
[`docs/GRADING.md`](GRADING.md#turn-bounds) § Turn bounds for the full semantics.

**The floor closes the transcript half of this hole; the state half is open.** A
`state_checks` block with no source the grading substrate can evaluate — one
carrying only `id_fields`, or only `db_probes`, or an empty `jsonpaths` list — is
scored a free `1.0` by core and recorded as not evaluated by the runner, so it
either contributes a passing component nothing earned or contributes nothing at
all. Either way it asks nothing of the agent. That is **#733**. Give a refusal task
at least one state assertion a wrong action would break: an idle agent and an agent
that acted wrongly must not come out with the same `state_checks` score.

---

## Designing Challenging Tasks

Tasks that always pass (100% success rate) provide zero RL training signal. Tasks that never pass (0%) are broken. Target **30-70% pass rate** for maximum training value.

### Anti-Patterns (make tasks trivially easy)

- **Step-by-step instructions in user messages.** "1. Navigate to website 2. Click button 3. Fill form" turns the agent into a script executor. Use natural language: "I need to update my shipping address."
- **UI defaults that satisfy grading.** If grading checks for "Apple Pay" and the checkout page defaults to Apple Pay, the agent doesn't need to do anything. Defaults should be DIFFERENT from the graded values.
- **System prompt escape hatches.** "If you can't do X, do Y instead" gives the agent permission to skip the hard part. The system prompt should describe capabilities, not workarounds.
- **Overly broad scripted_flow triggers.** Generic words like "done", "confirmed", "success" end the conversation before the agent finishes. Use specific triggers (order IDs, exact phrases) or use LLM user simulator.
- **LLM judge with high weight as a softener.** An LLM judge giving 0.7 for "attempted the task" masks state_checks failures. Reserve LLM judge for genuinely subjective evaluation.
- **JavaScript safety nets.** Code like `value || 'correct_answer'` means the graded value is always correct regardless of agent action. Record what actually happened.
- **Grading an idle agent cannot fail.** If the expected final state equals the initial state, a do-nothing agent matches it and passes; declare an activity floor — see [Refusal tasks and other do-nothing passes](#refusal-tasks-and-other-do-nothing-passes).

### Patterns for Effective Difficulty

- **Require active, non-default choices.** If a form has a default value, grade for a different value that requires explicit selection. Size "Large" when default is "Small". Payment "Apple Pay" when default is "Credit Card".
- **Natural language user messages.** Use LLM user simulator with a backstory that reveals information gradually, like a real person would.
- **Minimal system prompts.** Don't teach the agent the solution. Describe what tools are available, not how to use them for this specific task.
- **Multi-step reasoning.** Require the agent to gather information from one place and apply it in another (e.g., look up an order number in a PDF, then use it in a portal).
- **Strict state_checks grading.** Weight 1.0 on state_checks with exact-match assertions. No partial credit for vague attempts.
- **App-style browser tasks.** Use `initial_url` and `allowed_actions` to create phone-app-like experiences. Remove `navigate` and `open_web_browser` so the agent must interact with the UI, not bypass it with URLs.

### Calibration

After creating a task:

1. Run it 5+ times with the target model.
2. If pass rate is 100%: the task is too easy — add requirements, remove defaults, tighten grading.
3. If pass rate is 0%: the task is broken or impossible — verify the HTML flow works manually, check grading assertions match actual data format.
4. Target 30-70% for RL training value.
