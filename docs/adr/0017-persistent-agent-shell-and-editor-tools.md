# 0017. Persistent agent shell + first-class editor tools + tool-lifecycle evolution

- **Status:** Proposed
- **Date:** 2026-07-22
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tolokaforge ships two shell-tool flavours today, and **both are per-call
fresh-shell**:

- the in-process allowlisted `BashTool`
  (`tolokaforge/tools/builtin/bash.py`, `has_lifecycle=False`, 30 s timeout,
  regex allowlist), and
- the runner-side `DockerComposeExecToolWrapper`
  (`tolokaforge/runner/tool_factory.py`, `has_lifecycle=True`, per-call
  `docker compose exec bash -c`).

In both, **shell process state — cwd, exported environment, shell functions,
aliases, subshell state — resets between tool calls.** Only container-level
state (files written to disk, packages installed) survives from one call to the
next. An agent that runs `cd /srv && export TOKEN=…` in one call finds itself
back at the original cwd with `TOKEN` unset in the next.

Anthropic's standard agent-tool contracts assume the opposite. The `bash` tool
(`bash_20250124`) and the text-editor tool (`str_replace_based_edit_tool`) are
defined around a **session-lifetime** shell: state persists across calls, and a
`restart` command exists precisely because a fresh shell is otherwise never
obtained. Agents prompted or trained against those contracts burn turns
re-establishing state on tolokaforge's per-call shells, and there is **no
`str_replace_based_edit_tool` equivalent at all** — agents that expect a
first-class editor fall back to hand-rolled `sed`/`cat` heredocs through the
shell.

Separately, issue #63 flagged that the tool-lifecycle seam
(`ToolLifecycleContext` + `ToolWrapper.start()`/`stop()` in
`tolokaforge/runner/tool_factory.py`, driven off the `has_lifecycle` capability
in `tolokaforge/runner/service.py`) is a deliberate minimum-viable interface:
it was shaped around a single lifecycle tool and must be evolved the first time
a **second** lifecycle tool lands. This milestone is that second tool — a
session-lifetime persistent shell — and therefore the moment to evolve the
seam.

This ADR is the **design lock only — it introduces no runtime code and no
tests.** It defines, at interface level, the contracts that the implementing
sub-issues (A2–A6) each cite as their source of truth. Every named Decision
sub-section below `(a)`–`(g)` is a contract one sub-issue owns.

## Decision Drivers

- **Drop-in compatibility.** The stated milestone goal is that an agent written
  against Anthropic's `bash_20250124` + `str_replace_based_edit_tool` works
  against tolokaforge unchanged. The wire schema must match those contracts, not
  a homegrown approximation.
- **Fail-loud.** No silent fallbacks: an editor `str_replace` with an ambiguous
  or missing match, or a `create` onto an existing path, must error rather than
  guess.
- **The runner stays capability-driven.** Per ADR-0007, the runner drives tool
  lifecycle off the `has_lifecycle` capability, never off tool or adapter
  identity. The lifecycle evolution must preserve that invariant.
- **Additive, backward-compatible.** Existing `BashTool` consumers and the
  existing `DockerComposeExecToolWrapper` keep working byte-for-byte; new tools
  are opt-in.
- **Close #63 deliberately.** Evolve the lifecycle contract to exactly what the
  two shipping tools force, and triage the remaining speculative gaps with
  written rationale rather than reopening them reactively.

## Considered Options

1. **Adopt Anthropic's standard tool contracts verbatim** — a session-lifetime
   `bash_20250124`-shaped shell tool and a `str_replace_based_edit_tool`-shaped
   editor tool, each with base (local) and docker-compose provider variants.
2. **Design a homegrown persistent-shell / editor schema** tuned to tolokaforge's
   internals.
3. **Extend `BashTool` in place** to hold a long-lived subprocess.

## Decision

We adopt **Option 1**. The LLM-facing schema of both new tools matches
Anthropic's published contract exactly, so an agent prompted against the
standard tools needs no interface adaptation. Option 2 is rejected because a
homegrown schema reintroduces the interface-adaptation cost the milestone exists
to remove. Option 3 is rejected because `BashTool` is lifecycle-free and
in-process; a session-lifetime shell needs `start()`/`stop()`, which only a
`ToolWrapper` subclass exposes, and because the per-call allowlisted `BashTool`
is a compatibility surface that legacy callers still depend on.

The decision body is split into seven named contract sub-sections. Each is the
single source of truth for one implementing sub-issue.

### (a) Lifecycle-contract evolution — closes #63

`ToolLifecycleContext` stays a `@dataclass` in-process value object (the
AGENTS.md type-system table lists it `@dataclass(frozen=True)`; the concrete
live type is a plain `@dataclass` — whether to additionally freeze it is an
implementation call for the sub-issue that touches the code, not a design
constraint of this ADR). It is **not** promoted to Pydantic: it never crosses a
serialisation boundary. Its existing fields `trial_id: str` and
`artifacts_dir: str | None = None` are unchanged.

**The evolution this ADR locks:** the context gains exactly the per-trial facts
that the *runner* owns and that a tool cannot know at construction time.
Concretely, it adds **one field**:

```python
work_dir: str | None = None
```

`work_dir` is the session working root. The base (local) persistent-shell tool
seeds its shell `cwd` from it; the base editor tool validates paths against it.
All fields are additive and defaulted, so the existing construction in
`tolokaforge/runner/service.py`

```python
ToolLifecycleContext(trial_id=trial_id, artifacts_dir=…)
```

stays valid byte-for-byte.

**The ADR explicitly rejects** putting a `service` name, a `compose_file`
handle, or a runtime-backend handle into the context. Those are known at
tool-construction time from the tool's own config: `DockerComposeExecToolWrapper`
already takes `compose_file`/`service` as constructor arguments and derives its
per-trial compose project name from `ctx.trial_id` inside `start()`. Keeping
construction-time config out of the per-trial context preserves Core Rule 2 and
the ADR-0007 invariant that the runner drives lifecycle off the `has_lifecycle`
capability, never off tool or adapter identity.

**#63 gap triage.** Issue #63 enumerated six candidate evolutions of the
lifecycle seam. This ADR adopts one and defers five, each with a one-line
reason:

| # | Gap | Verdict | Rationale |
|---|-----|---------|-----------|
| 1 | Additive per-trial context field | **Adopt** | The persistent shell needs a session working root the tool cannot know at construction time. |
| 2 | Async `start_async` | Defer | Both shipping tools' `start()` is a fast synchronous subprocess spawn; nothing awaits I/O long enough to need async. |
| 3 | Lifecycle timeout / retry policy | Defer | A subprocess spawn either succeeds or raises immediately; there is no partial-readiness window to time out on. |
| 4 | Suspend / snapshot / reset hooks | Defer | Neither tool has resumable mid-trial state worth snapshotting; `restart` (shell) and per-call statelessness (editor) cover the reset needs. |
| 5 | Inter-tool ordering | Defer | The two tools are independent; neither `start()` depends on the other having started. |
| 6 | Explicit readiness signal | Defer | "`start()` returned" is a sufficient readiness signal for a synchronous spawn. |

Adopting gap 1 and triaging gaps 2–6 with reasons **is** what "closes #63"
means: the seam is evolved deliberately to what the shipping tools force, and
the speculative hooks are declined on the record rather than left open.

**Backward-compatibility guarantee (stated verbatim).**
`DockerComposeExecToolWrapper` behaviour is unchanged; the `has_lifecycle`
dispatch in `tolokaforge/runner/service.py` is unchanged; new context fields are
optional.

### (b) `PersistentShellTool` contract

This tool is anchored to Anthropic's **`bash` tool, type string `bash_20250124`**
— the same way `(c)` anchors the editor to `str_replace_based_edit_tool`.
`bash_20250124` is accepted by every current Claude model (the older
`bash_20241022` is computer-use-beta only). Drop-in compatibility with that
contract is the milestone's stated goal, so the ADR locks the exact wire schema
rather than a homegrown one.

**Input schema (locked).** Two fields, matching the Anthropic bash parameter
table:

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `command` | string | Required **unless** `restart` is set | The bash command to run. |
| `restart` | boolean | Optional | Set to `true` to restart the shell. |

A restart call sends `{"restart": true}` with **no** `command`. This is the
exact Anthropic bash schema, so an A3 implementer reading `(b)` alone has the
full wire contract.

**Tool shape.** The tool is a **`ToolWrapper` subclass with
`has_lifecycle=True`**, living in the runner tool-factory layer (analogous to
`DockerComposeExecToolWrapper`) — *not* a lifecycle-free `tools/builtin` tool
routed through the generic builtin wrapper, because only `ToolWrapper` exposes
`start()`/`stop()`. The ADR locks this constraint; the exact registration
mechanism (a new invocation style, a new registry dispatch kind, or a dedicated
factory method) is left to A3.

**Registry-name coexistence.** The LLM-facing schema matches `bash_20250124`,
but the tolokaforge registry name for this tool **must be distinct from the
legacy `bash` builtin** so both coexist: a task enables one or the other in its
`task.yaml`, and the old per-call `BashTool` stays for legacy callers per the
compatibility surface `(e)`. The ADR locks the constraint (distinct name,
additive coexistence); A3 picks the concrete name.

**Lifecycle.** `start(ctx)` opens one session-lifetime `bash` subprocess with
its `cwd` seeded from `ctx.work_dir`; `stop()` terminates it. State — cwd,
exported environment, functions, aliases, subshell state — persists across
`execute()` calls by construction of the long-lived process.

**Sentinel command-boundary detection.** Each command is written to the shell's
stdin followed by an echo of a per-command unique sentinel carrying the exit
status (an `__DONE_<uuid>__ $?` pattern); the reader consumes stdout up to the
sentinel line and parses the trailing exit code.

**Per-command timeout.** Default **120 s**, configurable via tool config
(`timeout_s`).

**Kill-safety.** On timeout the running command is killed (a signal to the
foreground command's process group) **without leaking the parent shell** —
subsequent commands still run in the same session.

**Output truncation.** 16 KB **middle-truncation**, with a grep-hint message
for the elided middle (the Anthropic convention).

**`restart` semantics.** `{"restart": true}` closes the current shell, opens a
fresh one, and returns a confirmation `tool_result`. Per the Anthropic contract
a restarted session **starts clean** — cwd, environment variables, and running
processes are all gone — and subsequent commands run in the new shell.

### (c) `StrReplaceEditorTool` contract

This tool implements Anthropic's **`str_replace_based_edit_tool`** spec: exactly
the four commands **`view` / `create` / `str_replace` / `insert`** and **no
`undo_edit`**.

`undo_edit` was **removed in the Claude-4 tool variants
(`text_editor_20250429`, `text_editor_20250728`); present only in the
pre-Claude-4 tools (`text_editor_20241022`, `text_editor_20250124`).** Because
tolokaforge targets current Claude models, the tool ships four commands:

| Tool `type` | Model era | Editor `name` | Commands |
|-------------|-----------|---------------|----------|
| `text_editor_20241022` | pre-Claude-4 | `str_replace_editor` | view, create, str_replace, insert, **undo_edit** |
| `text_editor_20250124` | pre-Claude-4 | `str_replace_editor` | view, create, str_replace, insert, **undo_edit** |
| `text_editor_20250429` | Claude-4 | `str_replace_based_edit_tool` | view, create, str_replace, insert |
| `text_editor_20250728` | Claude-4 | `str_replace_based_edit_tool` | view, create, str_replace, insert |

**Parameter names (locked):** `command`, `path`, `view_range`, `file_text`,
`old_str`, `new_str`, `insert_line`.

**Per-command semantics.**

- **`view`** — a file (optional `view_range` = `[start, end]`, **1-indexed**,
  `-1` for end = read to EOF; output is line-numbered) **or** a directory
  listing. Oversized files are 16 KB middle-truncated with a grep-hint.
- **`create`** — writes `file_text` to a new file; **fail-loud if the path
  already exists**.
- **`str_replace`** — replaces `old_str` (exact match, including whitespace)
  with `new_str`; **fail-loud on a non-unique match and on a missing match**.
- **`insert`** — inserts `new_str` at `insert_line`, the line number **after
  which** to insert; `insert_line=0` inserts before the first line.

**Path validation.** Reject symlinks that escape the configured working root.

**Lifecycle.** Stateless — `has_lifecycle=False`.

### (d) Docker-compose provider variants

The base tools in `(b)`/`(c)` run against the local host. Two docker-compose
provider variants run the same contract against a compose service:

- **`DockerComposePersistentShellTool`** — opens `docker exec -i <service> bash`
  in `start()`, holds it across the trial, closes it in `stop()`; `service`
  comes from tool config. Same shell contract as `(b)`.
- **`DockerComposeStrReplaceEditorTool`** — routes the four editor commands
  through `docker exec` (`cat` for reads plus an in-place mutation helper for
  writes). Same editor contract as `(c)`.

**Provider is a configuration axis, not a contract change.** The LLM-facing tool
schema is identical between a base tool and its compose variant; only *where the
bytes live* (local host vs. inside a compose service) differs. An agent cannot
tell which provider is behind the tool, and no sub-issue may let provider
identity leak into the schema.

### (e) Compatibility surface

The change is **additive** to public tolokaforge:

- `BashTool` (per-call, in-process, allowlisted) **stays** for legacy callers;
  new consumers choose the persistent tools.
- The lifecycle evolution in `(a)` preserves the existing
  `DockerComposeExecToolWrapper` byte-for-byte.

The migration surface — a CHANGELOG entry, the `docs/TOOLS.md` entries, and the
task-config field that enables each new tool — is owned by the *implementing*
PRs (A2–A4 for the code and the config field, A5 for `docs/TOOLS.md`). A1 edits
none of them; it only specifies the additive contract those PRs implement.

### (f) Docs + examples surface → A5

A5 delivers the documentation and examples surface for the new tools:
`docs/TOOLS.md` entries for the persistent shell and the editor (base and
compose variants), one or more example tasks that enable them, and the ADR index
row (Stage 2 of this issue adds the row; A5 owns `docs/TOOLS.md` and the
examples). The lifecycle-evolution doc update in `docs/RUNNER.md` is owned by the
PR that changes the lifecycle code (A2), not by A1 and not by A5.

### (g) Integration-test contract → A6

A6 delivers the end-to-end capstone: a real compose service against which
**shell state persistence** (cwd and environment surviving across calls) and
**editor round-trips** (create → view → str_replace → insert against a real
file) are asserted. Tier: **integration**.

## Consequences

### Positive

- **Agents built against the standard Anthropic tools work unchanged.** The
  primary milestone goal: no interface-adaptation turns, no homegrown schema to
  document.
- **A first-class editor replaces shell heredocs.** Agents get typed
  view/create/str_replace/insert with fail-loud match semantics instead of
  hand-rolled `sed`/`cat` through the shell.
- **The lifecycle seam is evolved once, deliberately.** #63 closes with an
  additive context field and five gaps triaged on the record, so the next
  lifecycle tool inherits a contract shaped by two real consumers rather than
  one.

### Negative / Trade-offs

- **A long-lived subprocess is more state to manage than a per-call exec.**
  Kill-safety on timeout (signalling the command's process group without leaking
  the parent shell) and clean `restart` are load-bearing; they are locked in
  `(b)` precisely because they are the hard parts.
- **Two more tools, each with a base and a compose variant, widen the tool
  matrix.** The `(d)` "provider is a config axis" rule keeps the LLM-facing
  schema single, but there are now more wrappers to maintain.
- **Registry-name coexistence is a small ongoing cost.** The persistent shell
  and the legacy `BashTool` share the Anthropic-facing shape but must keep
  distinct registry names indefinitely so tasks can select either.

### Follow-ups

- **Code changes required:** the lifecycle-context field and dispatch (A2), the
  persistent-shell tool + compose variant (A3), the editor tool + compose
  variant (A4).
- **Documentation to update:** `docs/RUNNER.md` for the lifecycle evolution
  (A2); `docs/TOOLS.md` and examples (A5); the ADR index row (Stage 2).
- **Tests to add:** the integration capstone (A6), plus the unit/contract tests
  each implementing PR owns for its own tool.

## Alternatives considered

- **Homegrown persistent-shell / editor schema.** Rejected: a bespoke schema
  reintroduces the interface-adaptation cost the milestone exists to eliminate,
  and gains nothing over the widely-supported Anthropic contract.
- **Extend `BashTool` in place to hold a long-lived subprocess.** Rejected:
  `BashTool` is in-process and lifecycle-free; a session-lifetime shell needs
  `start()`/`stop()`, which only a `ToolWrapper` subclass exposes. Mutating
  `BashTool` would also break the per-call allowlisted contract that legacy
  callers depend on.
- **Adopt all six #63 lifecycle evolutions now.** Rejected: five of the six are
  not forced by either shipping tool; adopting them speculatively locks in an
  interface shape ahead of a real second consumer. See the gap-triage table in
  `(a)`.
- **Put `service` / `compose_file` into `ToolLifecycleContext`.** Rejected:
  those are construction-time tool config, not per-trial runner facts; placing
  them in the context would couple the runner to tool identity and violate
  ADR-0007. See `(a)`.

## Links

- Related ADRs:
  - [ADR-0007](0007-runtime-backend-protocol.md) — `RuntimeBackend` Protocol
    (runner drives off capability, not identity)
  - [ADR-0016](0016-runtime-backend-comparison.md) — Runtime backend comparison
    (lifecycle axis)
  - [ADR-0018](0018-multi-container-under-shared-runtime.md) — Multi-container
    under shared runtime (composition axis)
- Related code:
  - `tolokaforge/runner/tool_factory.py` — `ToolLifecycleContext`, `ToolWrapper`,
    `DockerComposeExecToolWrapper`
  - `tolokaforge/runner/service.py` — `has_lifecycle` dispatch and
    `ToolLifecycleContext` construction
  - `tolokaforge/tools/builtin/bash.py` — the legacy per-call `BashTool`
- External references:
  - Anthropic bash tool (`bash_20250124`) and text-editor tool
    (`str_replace_based_edit_tool`) contracts.
