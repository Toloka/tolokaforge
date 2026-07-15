# Plan: issue #278 — A2 root `--version` + grouped `--help`

Issue: #278 (milestone 11, umbrella #297)
Branch: `feat/issue-278-a2-version-grouped-help` (already created off `feat/terminal-dx`)

## Context

The root `tolokaforge` command carries no `--version` flag today, and `tolokaforge
--help` prints one flat alphabetical block for eleven top-level names (`adapter`,
`analyze`, `assets`, `config`, `docker`, `prepare`, `run`, `status`, `validate`,
`worker`; plus the sub-groups). Milestone 11 (terminal DX) wants a familiar
`--version` verb and a `--help` layout that groups related verbs so operators
scan by concept — Runs / Tasks / Docker / Config / Assets / Adapters — instead
of by first letter.

## Goal

- `tolokaforge --version` prints `tolokaforge, version <installed-version>`
  where the version is read from installed package metadata (Click's default
  template + `importlib.metadata`).
- `tolokaforge --help` places every registered top-level command under a
  fixed-order group heading, alphabetical within each group.
- Every existing per-command `--help` (e.g. `tolokaforge run --help`,
  `tolokaforge docker --help`, `tolokaforge docker up --help`) renders
  identically to today — only the root group's `Commands:` section changes.
- Drift protection: registering a new top-level command without assigning it a
  group heading is a load-time failure with a clear message that names the
  command.

## Non-goals

- No renaming, adding, or removing top-level commands.
- No change to per-command flags, per-command help text, or the root callback's
  `-v/-q/--log-format/--display` options landed by A3+B2.
- No change to subgroup internals (`docker`, `config`, `adapter`, `assets`
  continue to render their own subcommands with Click's default flat list).
- No colouring / Rich rendering of the help output. Click's plain formatter
  stays.
- No shell-completion, no man-page generation.

## Interface

New module-private class in `tolokaforge/cli/main.py`:

```python
class _GroupedCommandsGroup(click.Group):
    """Click Group that renders `Commands:` as fixed-order group sections."""

    GROUP_ORDER: tuple[str, ...] = ("Runs", "Tasks", "Docker", "Config", "Assets", "Adapters")

    COMMAND_GROUPS: dict[str, str] = {
        "run": "Runs",
        "prepare": "Runs",
        "worker": "Runs",
        "status": "Runs",
        "analyze": "Runs",
        "validate": "Tasks",
        "docker": "Docker",
        "config": "Config",
        "assets": "Assets",
        "adapter": "Adapters",
    }

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None: ...
```

Behavioural contract of `format_commands`:

1. Enumerate visible commands via `self.list_commands(ctx)` (Click's usual
   filter — hidden commands stay hidden).
2. For each visible command name, look up its section in `COMMAND_GROUPS`. If a
   visible command is missing from the map, raise
   `RuntimeError("_GroupedCommandsGroup: no group heading for command '<name>'; "
   "add it to COMMAND_GROUPS")`. This is deliberate fail-fast — a new command
   without a group would silently disappear from `--help` otherwise.
3. Walk `GROUP_ORDER`. For each heading that has at least one command, emit a
   Click `formatter.section(heading)` block containing rows of
   `(command_name, short_help)` sorted alphabetically by `command_name`.
   `short_help` comes from `command.get_short_help_str(limit=formatter.width)`,
   matching Click's default.
4. Empty sections are skipped (no blank "Docker:" block if all Docker commands
   ever get hidden).

Wire-up on the root group:

```python
@click.group(cls=_GroupedCommandsGroup)
@click.version_option(package_name="tolokaforge")
@click.option("--verbose", "-v", ...)  # existing options unchanged
...
def cli(...): ...
```

`click.version_option(package_name="tolokaforge")` uses Click's default
template — `%(prog)s, version %(version)s` — and reads the installed version
via `importlib.metadata.version("tolokaforge")`. No custom `version=` string, so
the test asserts against `importlib.metadata.version("tolokaforge")` and stays
correct across `cz bump`.

## Stages

### Stage 1: `_GroupedCommandsGroup` + `--version` + tests

- **Contract:**
  - Introduce `_GroupedCommandsGroup(click.Group)` in `tolokaforge/cli/main.py`
    with the `GROUP_ORDER` tuple and `COMMAND_GROUPS` mapping shown above and a
    `format_commands(ctx, formatter)` override with the behaviour specified
    under "Interface" (fail-fast on unmapped, alphabetical within section,
    fixed section order, empty sections skipped).
  - Wire the root group as `@click.group(cls=_GroupedCommandsGroup)` and add
    `@click.version_option(package_name="tolokaforge")` immediately below the
    `@click.group(...)` decorator (Click orders version-option registration
    independently of other options).
  - No change to any `@cli.command(...)` decorator, any `cli.add_command(...)`
    call, or any callback signature. Every existing option, every existing
    help string, every existing subcommand tree stays byte-for-byte identical
    outside the root `Commands:` section.

- **Behaviour to lock** (tier: `unit`, new file
  `tests/unit/test_cli_help_grouping.py`, `pytestmark = pytest.mark.unit`, all
  invocations via `click.testing.CliRunner(mix_stderr=False)` on `cli` from
  `tolokaforge.cli.main`):

  1. `test_version_option_matches_importlib_metadata` — invoke `["--version"]`;
     assert `result.exit_code == 0` and
     `result.stdout.strip() == f"tolokaforge, version {importlib.metadata.version('tolokaforge')}"`.
     (Import `importlib.metadata` at the top of the test module; do not hard-code
     `0.8.3`.)
  2. `test_help_output_contains_group_headings` — invoke `["--help"]`; assert
     each of `"Runs:"`, `"Tasks:"`, `"Docker:"`, `"Config:"`, `"Assets:"`,
     `"Adapters:"` appears as a substring in `result.stdout`, and that the
     headings appear in that exact order (use `str.index` on each and assert
     monotonically increasing offsets).
  3. `test_help_output_places_commands_under_correct_headings` — invoke
     `["--help"]`; split `result.stdout` on the section headings and assert
     each command's name appears only in its assigned section. Explicit
     assertions: `run`, `prepare`, `worker`, `status`, `analyze` under `Runs:`;
     `validate` under `Tasks:`; `docker` under `Docker:`; `config` under
     `Config:`; `assets` under `Assets:`; `adapter` under `Adapters:`. Uses a
     substring window between the section heading and the next heading (or end
     of output) — no regex golden.
  4. `test_commands_within_section_are_alphabetical` — invoke `["--help"]`;
     extract the command names inside the `Runs:` section and assert the order
     is `["analyze", "prepare", "run", "status", "worker"]`.
  5. `test_every_registered_command_has_a_group` — pure introspection, no
     CliRunner. Import `cli` and `_GroupedCommandsGroup`; assert
     `set(cli.commands.keys()) <= set(_GroupedCommandsGroup.COMMAND_GROUPS.keys())`
     with a failure message that names the offending commands. This locks the
     drift-protection contract: a future contributor who adds a new
     top-level command and forgets to update `COMMAND_GROUPS` fails this test
     before ever running `--help`.
  6. `test_unmapped_command_raises_runtime_error` — build a bare
     `_GroupedCommandsGroup()` instance in the test, attach a dummy
     `click.Command("nope")` via `add_command`, invoke `["--help"]` through a
     `CliRunner`, and assert the invocation surfaces
     `RuntimeError` with the substring `"no group heading for command 'nope'"`
     (Click will propagate the raise; `standalone_mode=False` if needed to
     bubble it out).
  7. `test_subcommand_help_unchanged` — invoke `["run", "--help"]`,
     `["docker", "--help"]`, `["docker", "up", "--help"]`,
     `["config", "--help"]`, `["adapter", "convert", "--help"]`, and
     `["assets", "stamp", "--help"]`. For each: assert `exit_code == 0` and
     that `result.stdout` starts with `"Usage: "` and does **not** contain any
     of the six section headings (`"Runs:"`, `"Tasks:"`, etc.). This is the
     regression guard for the acceptance criterion "no behavioural change to
     existing subcommand `--help` output" — we are asserting that grouped
     formatting is confined to the root group, and that the per-command help
     bodies still render.

- **Compatibility:** the CLI surface is a compatibility surface (AGENTS.md
  Core Rule 5), but this stage is **additive** on both axes: `--version` is a
  new flag, and the root `--help` `Commands:` section reorders/re-headings its
  entries — no command is renamed, hidden, or removed, and no per-command
  flag changes. Shell scripts and CI that call subcommands are unaffected.
  Scripts that grep the root help output for a command name still match
  (the names are unchanged); scripts that grep the specific string `"Commands:"`
  would break — we accept that since no shipped tooling does this and the
  new headings are the more scannable form.

- **Deliverable:** one commit containing the class + wire-up + test file.
  No other file touched.

- **Validation:**
  - `mcp__dev__run_tests marker="unit" path="tests/unit/test_cli_help_grouping.py"`
    passes.
  - `mcp__dev__run_python code="from click.testing import CliRunner;
    from tolokaforge.cli.main import cli;
    r = CliRunner(mix_stderr=False).invoke(cli, ['--help']);
    print(r.stdout)"` shows the six group headings in the specified order.
  - `mcp__dev__run_python code="from click.testing import CliRunner;
    from tolokaforge.cli.main import cli;
    r = CliRunner(mix_stderr=False).invoke(cli, ['--version']);
    print(repr(r.stdout))"` shows `tolokaforge, version <version>\n`.
  - Reviewer will additionally check: (a) no `@cli.command` decoration lost
    its options, (b) `_GroupedCommandsGroup` is prefixed `_` (module-private),
    (c) `COMMAND_GROUPS` values match `GROUP_ORDER` entries (no typos).

- **Doc updates:** none in this stage. All documentation lands in Stage 2 so
  the doc commit is self-contained and easy to skim.

### Stage 2: `docs/CLI.md` + CHANGELOG

- **Contract:** documentation only.

- **Behaviour to lock:** none — this stage adds no code.

- **Compatibility:** N/A.

- **Deliverable:** one commit updating two files.

- **Doc updates:**
  - `docs/CLI.md` — add a new top-level `## Root help layout` section
    (positioned between the existing `## Display modes` section at line 120 and
    `## stdout / stderr contract` at line 193). The section reads as if the
    grouped layout is the only layout that has ever shipped — no "previously
    flat, now grouped" phrasing (AGENTS.md Core Rule 8). Content:
    1. One paragraph naming the six sections in fixed order and the
       alphabetical-within-section rule.
    2. A code fence showing an abbreviated `tolokaforge --help` transcript
       (the `Commands:` block only) so readers see the exact heading strings.
    3. One paragraph on `tolokaforge --version` — states it prints
       `tolokaforge, version <version>` sourced from
       `importlib.metadata.version("tolokaforge")` and cites Click's
       `version_option`.
    4. One paragraph on the drift-protection contract: adding a new top-level
       command requires an entry in `_GroupedCommandsGroup.COMMAND_GROUPS`; a
       unit test enforces this. This paragraph exists so future contributors
       find the constraint without reading `main.py`.
  - `CHANGELOG.md` — append under `## Unreleased` → `### Feat`:
    ```
    - **cli**: `tolokaforge --version` prints `tolokaforge, version <version>`
      (sourced from installed package metadata). `tolokaforge --help` now
      groups top-level commands under fixed-order headings: **Runs**
      (`analyze`, `prepare`, `run`, `status`, `worker`), **Tasks**
      (`validate`), **Docker** (`docker`), **Config** (`config`), **Assets**
      (`assets`), **Adapters** (`adapter`). Per-command `--help` unchanged.
      See [docs/CLI.md](docs/CLI.md) § Root help layout. (#278)
    ```

- **Validation:**
  - `rg -n "previously|used to|before the refactor|now grouped" docs/CLI.md`
    returns no hits in the new section (Core Rule 8 spot-check).
  - `rg -n "flat --help|alphabetical --help" docs/CLI.md` returns no hits
    (no history phrasing).
  - Reviewer will scan for a stale "Commands:" block anywhere else in the
    repo (`rg -n "^Commands:" docs/`).

## Discovered issues

- **Fix in this PR:** None. The scope is intentionally narrow — grouped help +
  `--version`. Any adjustments to per-command short-help strings or flag
  descriptions belong in their own tickets.

- **Filed as issues:** None. During discovery I saw no smell that warrants a
  separate ticket in the surrounding CLI code. The A3/A4/B1/B2 landings look
  clean.

## Risks / open questions

- **Click version pinning of `formatter.section`.** `click.HelpFormatter.section`
  is a public context manager stable since Click 7. The repo pins Click via
  `pyproject.toml`; Stage 1's implementer should confirm the pinned version
  exposes `section`. If a future major Click ever moves to a different helper,
  the fix is one-line inside `format_commands` and is localized to
  `_GroupedCommandsGroup` — no downstream code depends on it.
- **`--version` colouring under `--display`.** `click.version_option` prints
  through Click's own channel (stdout, no Rich). This is intentional: the
  version string is a machine-friendly artifact (same discipline as A4's
  "stdout is artifact" rule) and must not be redirected to stderr or coloured.
  Stage 1's test asserts on `result.stdout` precisely to lock this. No open
  question — flagging only so the reviewer does not "helpfully" route the
  version through the shared `console`.
- **Alphabetical ordering of `analyze` inside `Runs`.** The intuitive user
  story ordering would be `run → prepare → worker → status → analyze` (workflow
  order), but the issue asks for alphabetical within group, which places
  `analyze` first. Sticking with alphabetical because (a) the issue specifies
  it, (b) any "workflow order" is arbitrary the moment a new verb is added,
  and (c) alphabetical is what Click's default group formatter does today so
  users' muscle memory (`Ctrl-F "run"`) still works.
