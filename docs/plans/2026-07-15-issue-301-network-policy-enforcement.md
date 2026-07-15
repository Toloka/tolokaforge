# Plan: network_policy enforcement + tests (Multi-container 3/5)

Issue: #301 (umbrella #304)
Branch: feat/network-policy-enforcement

## Context

**Audit result — enforcement is entirely absent for task-declared stacks; the capability advertisement is false.**

`network_policy` is declared on `EnvironmentPatch` / `EnvironmentManifest`
(`tolokaforge/runner/models.py:958`, `:1035`), validated as a closed enum
(`no_internet` | `limited_internet` | `full_internet`, default `no_internet`),
and resolved onto the manifest by `tolokaforge/core/project_loader.py`. From
there it is **read nowhere**. `rg network_policy` returns only the model
definitions, the resolver, docs, and one example pack — **zero** hits in the
docker-provisioner path (`compose_materialisation.py`, `shared_stack_runtime.py`,
`per_trial_runtime.py`, `tolokaforge/docker/`).

Both backends nonetheless **advertise** `network_isolation:no_internet` as an
honoured capability (`shared_stack_runtime.py:698`, `per_trial_runtime.py:113`;
registered at `backend_capabilities.py:63` with the description "Backend
enforces per-trial network isolation with no public egress"). The admission gate
(`check_admission`) will happily admit a run that requires it — giving operators
false confidence. `docs/TASKS.md:99` is at least honest: "`network_policy` —
reserved. Default `no_internet` is documented but not enforced."

The materialisation path in both backends is identical:
`copy_compose_context(manifest.compose_file, temp_dir)` →
`DockerCompose(context=temp_dir, compose_file_name=…, …)` → `compose.start()`
(`shared_stack_runtime.py:835-843`, `per_trial_runtime.py:199-207`). The task's
compose file is run **verbatim**; whatever networks it declares (or the compose
default bridge) are used, all egress-capable.

The `internal=True` flag on `tolokaforge/docker/network.py`'s `Network.create`
exists but is dead code — the only production caller (`docker/stack.py:411`, the
Case A `EngineStack`) creates its networks **without** it, and the testcontainers
`DockerCompose` path never touches `Network` at all.

**Reproduced by running (2026-07-15, real Docker 24.0.5).** A two-service compose
(`alpine` + `nginx`, default network) reaches the open internet today:
`ping 8.8.8.8` → `EGRESS_OK`, `wget http://1.1.1.1` → `EGRESS_OK`. This is the
leak: a task pack declaring `no_internet` is silently granted full internet.

**Enforcement mechanism validated by running.** Marking the compose default
network `internal: true` blocks egress (`EGRESS_BLOCKED`) and preserves
inter-service traffic — **but drops the host-published port** (`docker compose
port` → NONE), which the backend depends on to resolve and reach the runner's
gRPC endpoint. A naive "mark the default network internal" therefore breaks
Case B/C materialisation entirely.

The working topology (validated by running) is a **two-network split**: every
service joins an `internal: true` network (no egress, inter-service DNS intact),
and the runner service *additionally* joins a non-internal "edge" network.
Result: `host->runner HTTP 200` (backend resolves + reaches the runner),
`INTERSVC_OK` (runner → app-service by DNS), `APP_EGRESS_BLOCKED` (application
services cannot egress), `RUNNER_EGRESS_OK` (runner keeps egress).

The runner keeping egress is **required by design, not a leak**: the runner runs
LLM-as-judge in-container (`tolokaforge/runner/service.py:1321`, keys exported at
`runner/__main__.py:91`) and must reach the LLM provider to grade. So the honest
`no_internet` contract is: *task-declared application services have zero public
egress; the engine runner retains its control-plane + grading egress.*

## Goal

Make `manifest.network_policy` actually parameterise the docker provisioner for
task-declared stacks (Cases B and C), turning the existing
`network_isolation:no_internet` capability advertisement truthful:

- **`no_internet`** — every task service on an `internal: true` network (no
  public egress); runner additionally on a non-internal edge network so its
  published gRPC port stays host-reachable and it retains judge egress.
- **`full_internet`** — compose run unchanged (current behaviour).
- **`limited_internet`** — **fail loud** at materialisation. Docker's `internal`
  flag is binary; a real allowlist needs an egress-proxy sidecar (filed as
  #323). Refusing to run is the only honest option — silently granting full or
  no internet would under/over-enforce a declared security posture.

## Non-goals

- `limited_internet` allowlist enforcement (egress proxy) — #323.
- Network posture for the built-in Case A `EngineStack` — #324.
- Blocking egress of agent tools that execute inside the runner — #325.
- Any new `network_policy` enum value (boundary: does not extend the set).
- Kubernetes / non-docker substrates.

## Stages

### Stage 1: Enforcement transform + backend wiring

- **Contract:** New pure transform in `tolokaforge/core/compose_materialisation.py`:

  ```python
  def enforce_network_policy(
      compose_doc: dict[str, Any],
      policy: NetworkPolicy,
      runner_service: str,
  ) -> dict[str, Any]:
      """Return a compose doc rewritten to enforce `policy`."""
  ```

  Semantics (interface, not implementation):
  - `full_internet` → returns the doc unchanged.
  - `no_internet` → the returned doc guarantees: (1) **every** service is
    attached to a network with `internal: true` (any network the task already
    declared is also forced `internal: true` — no task service may egress);
    (2) `runner_service` is *additionally* attached to a non-internal edge
    network. Use distinctive injected network names (e.g.
    `tolokaforge_netpolicy_internal` / `tolokaforge_netpolicy_edge`) to avoid
    collision with task-declared networks; compose prefixes them with the
    per-run/per-trial project name, so they are unique on the daemon.
  - `limited_internet` → the transform is **not** the enforcement point for this
    value (see the pre-check below). As belt-and-suspenders it must still refuse
    `limited_internet` — raise the same `NetworkPolicyError` — so a caller that
    reaches the transform with `LIMITED_INTERNET` (a wiring bug) fails loud
    rather than silently returning an unmodified, egress-capable doc.
  - `runner_service` is guaranteed present in `services` by
    `EnvironmentManifest` validation — trust it, no defensive branch.

  **Fail-loud pre-check (the enforcement point for `limited_internet`).** Add a
  new module-level helper that reads *only* `manifest.network_policy` (no compose
  I/O) and raises `NetworkPolicyError` for `LIMITED_INTERNET`, naming #323 and the
  two enforced values:

  ```python
  # tolokaforge/core/compose_materialisation.py
  class NetworkPolicyError(ValueError): ...

  def verify_network_policy_supported(policy: NetworkPolicy) -> None:
      """Raise NetworkPolicyError if `policy` has no docker enforcement yet.
      limited_internet needs the #323 egress-proxy; refuse rather than
      silently grant full/no internet."""
  ```

  **Wiring (both backends).** `copy_compose_context` sits *inside* the compose-up
  `try` in both backends (`shared_stack_runtime.py:834-835`,
  `per_trial_runtime.py:198-199`), so a transform that reads the copied file can
  only run inside that `try` and its raise would be swallowed by the
  `except Exception` (shared `:844`, per-trial `:208`) and re-wrapped as a
  misleading `ProvisionError("docker compose up failed…")`. Resolve this by
  splitting enforcement across the try boundary:
  - **Before** the `try` (immediately before `temp_dir = make_project_temp_dir(...)`):
    call `verify_network_policy_supported(manifest.network_policy)`. It needs no
    compose read, so `limited_internet` fails with its own clear
    `NetworkPolicyError` *before any docker work or temp-dir creation*.
  - **Inside** the `try`, right after `copy_compose_context`: read the copied
    compose file, apply `enforce_network_policy`, write it back, then construct
    `DockerCompose(...)`. For `no_internet` / `full_internet` this rewrite cannot
    raise `NetworkPolicyError` (the pre-check already excluded the only refusing
    value); a genuine I/O error here correctly surfaces as `ProvisionError`,
    matching today's copy-failure semantics (option (a): no change to
    copy-context error semantics).

  A small file-level wrapper (read → `enforce_network_policy` → write) may live
  beside the pure transform; the pure transform is the unit-testable core.

- **Behaviour to lock:**
  - `unit` — `enforce_network_policy` on a minimal compose doc: `no_internet`
    yields internal-marked networks on every service + edge on runner;
    `full_internet` is identity; `limited_internet` raises `NetworkPolicyError`
    (belt-and-suspenders). Include a doc that **already declares its own
    `networks:`** — assert those are forced `internal: true` and the runner
    still gains the edge network.
  - `unit` — `verify_network_policy_supported`: raises `NetworkPolicyError` for
    `LIMITED_INTERNET`, returns cleanly for `NO_INTERNET` and `FULL_INTERNET`.
  - `canonical` — snapshot the transformed compose doc for the
    `multi_service_example_01` compose under `no_internet` (pins the exact
    injected topology so a future refactor cannot silently change the wire shape).

- **Compatibility:** Internal only — `enforce_network_policy` is a new internal
  seam; no task-pack, config, CLI, or public-API surface changes. The
  `network_policy` field, its enum, and the manifest wire shape are unchanged.
  Note in the PR body that enforcing the *default* `no_internet` changes the
  observable network posture of every existing task-declared stack (previously
  unenforced) — this realises the already-documented default, not a contract break.

- **Deliverable:** `manifest.network_policy` parameterises both backends; the
  `network_isolation:no_internet` advertisement is now truthful.

- **Validation:** `mcp__dev__run_tests` markers `unit` + `canonical`;
  `mcp__dev__lint_check`. Reviewer checks the transform is a pure function; the
  `verify_network_policy_supported` pre-check is hoisted **before** the
  compose-up `try` in both backends (so the `limited_internet`
  `NetworkPolicyError` escapes the `except` and is not re-wrapped as
  `ProvisionError`); the doc-rewrite sits inside the `try` right after
  `copy_compose_context`; and no model-name / policy-string branching leaks
  outside this transform.

- **Doc updates:** None in this stage (docs land in Stage 3 as one coherent rewrite).

### Stage 2: Integration proof — real egress enforcement

- **Contract:** New `tests/integration/network_policy/` (mirrors
  `tests/integration/reset_recipes/`), marker `integration`. Reuse the existing
  Case B/C harness pattern (`test_example_microservices_pack.py`,
  `tests/integration/reset_recipes/*`): materialise a task-declared stack via
  the real backend and assert observable network behaviour. The fixture stack
  needs a service the test can exec a curl/wget from (an `alpine`/`curl` service
  is sufficient; no LLM keys required).

- **Behaviour to lock (`integration`):**
  - `test_no_internet.py` — a `no_internet` stack: an application service's
    `curl`/`wget` to a public host (raw IP `http://1.1.1.1` and a DNS name) fails
    with a connection/network-unreachable error (or times out cleanly); the
    runner's published gRPC port is still resolvable and reachable from the host;
    inter-service DNS still works.
  - `test_full_internet.py` — the same stack under `full_internet`: the public
    request succeeds (guards against over-enforcement regression).
  - `test_limited_internet.py` — materialising a `limited_internet` stack raises
    `NetworkPolicyError` (surfaced before any container starts).

- **Compatibility:** Internal (tests only).

- **Deliverable:** The ship condition is met and pinned: `no_internet` curl
  fails, `full_internet` curl succeeds, `limited_internet` refuses to run.

- **Validation:** `scripts/with_env.sh uv run pytest tests/integration/network_policy -v`
  (or dev MCP `run_tests` marker `integration`). **Regression sweep (required):**
  re-run the existing task-declared-stack integration tests under the new
  default-`no_internet` behaviour —
  `tests/integration/test_example_microservices_pack.py`,
  `tests/integration/reset_recipes/`,
  `tests/integration/test_reset_recipe_end_to_end.py`,
  `tests/integration/test_cross_mode_isolation.py`,
  `tests/integration/docker/test_per_trial_runtime_backend_integration.py`.
  Audit shows these reach non-runner services via in-network DNS (not host
  ports) and need no egress, so they should pass unchanged; confirm by running.

- **Doc updates:** `tests/README.md` if the network_policy suite needs a marker
  or fixture note (only if it introduces a pattern not already covered).

### Stage 3: Docs + capability truthfulness

- **Contract:** Documentation reflects the enforced contract as the only state.

- **Behaviour to lock:** none (docs). A `canonical` guard already asserts the
  wire enum (`test_environment_manifest_contract.py:467`); leave it.

- **Compatibility:** Docs are a source-of-truth surface (AGENTS.md Core Rule 8) —
  rewrite to current state, delete legacy "not enforced" mentions.

- **Deliverable + doc updates:**
  - `docs/architecture/adr/0018-multi-container-under-shared-runtime.md` — add a
    "Network policy enforcement" section: the internal+edge two-network topology,
    the three policy values, why the runner keeps egress (judge), and the scoped
    contract (application services, not runner-executed tools — cross-ref #325).
  - `docs/TASKS.md:99` — rewrite the `network_policy` line so it reads as an
    enforced field (no_internet / full_internet enforced, limited_internet
    refused pending #323); delete "documented but not enforced".
  - `docs/architecture/adr/0010-runtime-backend-provisioning-contract.md:151-153`
    — reconcile the "Enforce via substrate-native network isolation" rows with
    the shipped mechanism (name the internal-network + edge split); the
    `limited_internet` row points at #323.
  - `rg network_policy docs/` and reconcile any remaining "reserved / not
    enforced" phrasing (esp. `docs/architecture/PROJECTS.md`); the "policy
    request" vocabulary framing stays accurate and should be left intact.
  - `tolokaforge/core/backend_capabilities.py:64` — verify the
    `network_isolation:no_internet` description still reads true now that
    enforcement is real (adjust wording if it over-claims per-trial specificity).

- **Validation:** `mcp__dev__run_tests` marker `canonical` (manifest contract
  still green); manual doc read.

## Discovered issues

- **Fix in this PR:** The false `network_isolation:no_internet` capability
  advertisement — made truthful by Stage 1 (no separate issue; it *is* the fix).
- **Filed as issues:**
  - #323 — `limited_internet` egress-allowlist enforcement (proxy sidecar).
    This PR fails loud on `limited_internet` until #323 lands.
  - #324 — built-in Case A `EngineStack` ignores network posture; `internal=True`
    on `Network.create` is otherwise-dead code.
  - #325 — runner-executed tool egress is not blocked under `no_internet`
    (design-note; runner egress is required for judge, so the contract is scoped
    to application services).

## Risks / open questions

- **Default `no_internet` now bites every task-declared stack.** Previously
  unenforced; now every Case B/C run without an explicit `full_internet` gets
  egress-blocked application services. Audit indicates existing examples/tests
  are self-contained (no egress, no non-runner host-port dependence), but Stage 2's
  regression sweep is the gate — if any example legitimately needs egress it must
  declare `network_policy: full_internet` explicitly.
- **`db-service` loses its host-published port under `no_internet`** (internal-only).
  The resolve path already tolerates `db_url=None` (`shared_stack_runtime.py:867`;
  the runner reaches db-service by in-network DNS via `DB_SERVICE_URL`), so this is
  expected, not a regression — confirmed by the audit that no test resolves the
  db host port.
- **CI does not run integration tests by default** (need Docker + the gated job).
  Stage 2's proof runs in the integration lane; the fast unit/canonical gate
  covers the transform. Reviewer should confirm the integration job is exercised
  before merge.
- **Injected network-name collision** with a task that already declares a network
  of the same name — mitigated by distinctive names; the transform should treat a
  pre-existing same-named network as its own (idempotent) rather than error, but
  this is an implementation detail for the implementer to settle with a unit test.
