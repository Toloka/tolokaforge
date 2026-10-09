# 0058. An application world served over HTTP holds one trial's state: `initial_state.app_world`, runtime-minted credentials, engine-side grading

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** — (proposed by @rsmtnn)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Vendor-shaped mocks of real services (Zendesk, Gmail, Mambu, …) are shared as a
library (ADR-0056). A native pack reaches them through its MCP server. The
server's subprocess holds the world and belongs to the trial: the runner reads
the world back with `_tolokaforge_get_state_` before grading and restores it
with `_tolokaforge_set_state_` before the golden replay. That path needs none
of the changes below.

A compose task whose agent calls the vendors' REST APIs with `http_request` has
no such seam. The world lives in a service of the stack, and today nothing:

- loads the task's world into that service;
- reads it back for `state_checks`;
- gives the golden replay a world to run in;
- ensures that two concurrent trials do not share the service's state.

`mock-web` illustrates the last point: its state is one module-level dict
behind a global `/api/reset`.

The REST APIs also identify the caller by credentials. `http_request`
deliberately drops every header except `Content-Type`, `Accept` and
`User-Agent`, so the agent cannot present a credential. That rule must
survive. Any credential that reaches a request has to come from the runtime,
must never be written into a task file, and must never become visible to the
model.

## Decision Drivers

- **One world per trial, enforced.** Isolation is checked, not assumed. A
  configuration that would let two trials share a world is refused.
- **No credentials in task configuration or on the model's side.** The agent's
  header scrub stays. Credentials for mock services are generated per trial.
  Credentials for real services exist only as `SecretManager` references.
- **One grader.** The engine grades: db-service tables, `state_checks.hash`
  with `golden_actions`, `compare_columns`, `comparison_view` (ADR-0053),
  `jsonpaths`. The service provides state, not verdicts.
- **The engine knows a protocol, not a library.** Any service that answers the
  administration protocol can hold a world.
- **No change for packs that do not declare it**, and no wire field that an
  older image refuses unless a pack uses it.

## Considered Options

1. **The runner administers a trial-scoped world service, and the runtime mints
   the credentials.** `initial_state.app_world` names the service. The runner
   loads, reads back and restores the world, as it does for an MCP server.
   Tokens are generated per trial and attached by `http_request`.
2. **As 1, with tokens written in the task.** `task.yaml` lists bearer tokens
   per actor and the header values `http_request` attaches.
3. **Grade the service with `db_probes`-style probes** against an inspection
   endpoint, and leave loading and replay to the pack.
4. **Run the world in the runner process** and reach it over the loopback from
   `http_request`.
5. **One run-scoped service holding many worlds keyed by trial**, with the
   trial's key carried in its credentials.

## Decision

We will adopt **Option 1**.

- Option 2 is rejected. It writes credentials into task files, in two places,
  and nothing would stop a real key from being written there.
- Option 3 is rejected. It leaves the golden replay without a world, and each
  pack would re-implement loading.
- Option 4 is rejected. It ties the world's dependencies to the runner image
  and cannot serve a world that another container (a user simulator's tool, a
  test runner) must also reach.
- Option 5 is deferred. It saves a container per trial but needs a
  multi-world protocol. It can be added later behind the same declaration.

### Declaration

```yaml
initial_state:
  json_db: initial_state.json          # the world as tables
  app_world:
    url: http://appmocks:8080          # a service of the task's stack
    hosts:                             # vendor hosts this service answers for
      - aldermere.zendesk.com
      - gmail.googleapis.com
    actors:                            # tool actor -> world caller (null: the seed's default caller)
      agent: null
      user: customer
```

No credential appears in the declaration. Task validation refuses:

- `app_world` without `json_db`;
- `app_world` next to a `tools.<actor>.mcp_server` (one state holder per trial);
- a host in `hosts` that is not in that actor's `http_request.allowed_hosts`.

### Isolation

The service that `url` names must have `isolation: ephemeral` or
`isolation: reset` in the environment manifest (ADR-0018, ADR-0044). Validation
refuses `shared` and names the service. Under these isolations the runtime
already gives each trial its own stack, so concurrent trials never share a
world. A run-scoped world service (Option 5) would need its own ADR.

`mock-web` is unchanged. `app_world` does not route through it, and its shared
state stays a known limitation of `mock-web`. A task that needs per-trial HTTP
state should use `app_world`.

### Administration protocol and lifecycle

The service answers, behind `X-Admin-Token`:

- `PUT /_admin/tokens` (`{token: caller}`);
- `PUT /_admin/tables`, `GET /_admin/tables` (the world as tables).

`GET /_health` answers without a token, so the stack's health check can reach
it. Until tables are loaded, every vendor request is answered `503`.

| Stage | What the runner does |
|---|---|
| `RegisterTrial` | Loads the tokens, then `json_db`'s tables. Fails the registration if the service refuses. |
| `GetState`, `GradeTrial` | Read the tables back into the db-service before grading. |
| Golden replay | Restores the initial tables before it runs. |
| `ResetTrial` | Restores the initial tables. |

Golden actions are `http_request` calls, replayed through the agent's own tool
and therefore under the agent's own credentials.

### Credentials

- **Minting.** At `RegisterTrial` the runner generates a random admin token and
  one bearer token per declared actor. It admits each value with
  `register_runtime_secret`, so the global log redactor masks them, and loads
  `{token: caller}` into the service.
- **Delivering the admin token.** The compose stack starts before
  `RegisterTrial`, so a token minted then cannot be written into the
  service's command or environment. The service therefore starts with no
  admin token and **claims the first one presented**: the first
  `PUT /_admin/tokens` that carries `X-Admin-Token` binds that value, and from
  then on every `/_admin/*` call must present it (`403` otherwise). The
  runner's `RegisterTrial` is that first call. The agent cannot win the race:
  `http_request` drops `X-Admin-Token` like every other header, and the agent
  takes no turn before `RegisterTrial` returns. A claim the runner loses
  (another container of the stack claimed first) fails the registration
  instead of grading a world someone else controls. The alternative, minting
  on the host before `compose up` and passing the token through
  `container_secrets_env`, closes even that window but needs the compose
  materialisation to know which service receives it. It stays open as a
  follow-up if the window proves to matter.
- **Attaching.** `http_request` is built once per actor. It first drops the
  agent's headers, as today. It then sets `Authorization: Bearer <token>` only
  on requests whose host is in `app_world.hosts`. Requests to any other host
  carry no credential. The token never appears in the tool's schema, its
  arguments, its output or the trajectory.
- **Vendor auth schemes.** The vendor's own scheme (Basic, `apikey`, form
  `token`) is the service's concern. A world service accepts the runtime bearer
  on every host it serves. Because the agent's headers are dropped, the
  vendor's scheme as the agent sees it does not change.
- **Real services.** Credentials for real services are out of scope.
  `http_request` never attaches a credential to a host outside
  `app_world.hosts`. If a later task needs one, its value is a
  `${secret:NAME}` reference resolved through `SecretManager`, as LLM proxy
  headers already are (`core/llm/proxy.py`). That needs its own ADR.

### Grading

The world's tables enter the db-service like an MCP server's state, so
`state_checks.hash`, `compare_columns`, `comparison_view` (ADR-0053) and
`jsonpaths` apply unchanged. The service and the library behind it return no
verdict. A library may publish a recommended `grading.yaml` fragment per
application in ADR-0053's vocabulary, for example:

- `normalize_ids` for sequential ids that other tables reference;
- `exclude_records` for superseded revisions.

Pack authors copy that fragment; the engine does not read it from the library.

A world whose clock and id sequence come from the seed makes one trajectory's
state reproducible. It does not make `normalize_ids` unnecessary: two valid
trajectories that create records in a different order still number them
differently.

### Wire

`app_world` crosses the wire on `RunnerInitialStateConfig` and is left out of
the dump while absent. An image without the field therefore accepts every pack
that does not declare it.

## Consequences

### Positive

- A compose task grades its world by state hash and golden path, as an MCP pack
  does, while the agent sees the vendors' real URLs and status codes.
- The same world tables serve an MCP pack (in process) and a compose pack (as a
  service) without changing their grading.
- No credential lives in a task file. The agent cannot authenticate as another
  actor or reach the administration endpoints. Leaked logs carry no usable
  token, and tokens expire with the trial.

### Negative / Trade-offs

- A world per trial costs a container. The in-process MCP path stays the
  default for packs that do not need HTTP.
- Core `GradingEngine` on the host cannot replay against a service. It refuses
  the golden replay of such a task as unbuildable, as it does for a task without
  an MCP server. These trials are graded by the runner.
- The log redactor matches substrings only. A token inside a Base64 or
  URL-encoded form is not masked, so the runtime must never log request
  headers.
- A world service must accept the runtime bearer on every host it serves, in
  addition to the vendor's scheme, and must support the first-claim admin
  token. `appmocks` does neither in full today; both are library changes.
- Between `compose up` and `RegisterTrial`, any container of the stack could
  claim the admin token first. The runner then fails the registration, so the
  window costs a failed trial, not a wrong grade.

### Follow-ups

- Code changes required:
  - `InitialStateConfig.app_world` and its validation (`json_db`, no
    `mcp_server`, hosts within `allowed_hosts`, isolation not `shared`);
  - the runner's state holder for `app_world` (load, read back, restore, reset);
  - per-trial token minting through `register_runtime_secret`;
  - credential attachment in `tools/builtin/http_request.py`;
  - `RunnerInitialStateConfig.app_world` with conditional serialisation;
  - the authoring gate counting `app_world` as a replayable world.
- Found by a prototype, needed whatever is decided here, and filed as
  separate bugs:
  - the `no_internet` network policy must keep service aliases, otherwise a
    vendor hostname that a world service answers for resolves to the real
    vendor ([#1835](https://github.com/Toloka/tolokaforge/issues/1835));
  - `http_request` must return the body of a 4xx/5xx response to the agent
    ([#1836](https://github.com/Toloka/tolokaforge/issues/1836));
  - the runner image must ship `core/tools_interface.py`, which every MCP
    pack built on `create_server` imports
    ([#1834](https://github.com/Toloka/tolokaforge/issues/1834)).
- Documentation to update: `PROJECTS.md`, `MULTI_CONTAINER_GUIDE.md`,
  `TOOLS.md`, `TASK_DESCRIPTION_SCHEMA.md`.
- Tests to add:
  - one golden path graded identically through an MCP pack and a compose pack;
  - concurrent trials against per-trial stacks keep separate worlds;
  - `shared` isolation is refused;
  - no token appears in the trajectory, tool output or logs;
  - no credential is attached to a host outside `app_world.hosts`.

## Links

- Related ADRs: [0018](0018-multi-container-under-shared-runtime.md) and [0044](0044-composition-plan-runtime.md) (compose stacks, isolation), [0029](0029-build-check-builtin-tool.md) (peer-service probes), [0053](0053-comparison-view-before-the-state-hash.md) (comparison view), [0056](0056-shared-tool-libraries-through-tool-artifacts.md), [0057](0057-mutates-state-on-the-tool-wire.md).
- Related code: `tools/builtin/http_request.py` (`_scrub_headers`), `core/tools_interface.py` (`_tolokaforge_get_state_`/`_tolokaforge_set_state_`), `secrets/manager.py` (`register_runtime_secret`), `secrets/expand.py` (`${secret:NAME}`), `env/mock_web_service/app.py`.
- External references: `appmocks serve` (`toloka-partners/app-mocks`), whose HTTP facade answers this protocol.
