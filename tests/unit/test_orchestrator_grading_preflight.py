"""No trial is paid for against a pack whose grading cannot be graded.

The three authoring hazards are silent at grade time — a misspelled tool name in a
``present`` matcher scores the component ``0.0`` with the message a genuine agent
failure carries, the same typo under ``absent`` passes every trial, and a removed
grading key is only rejected while artifacts are written, after the agent has run.
So a run puts every selected task through the gate ``tolokaforge validate`` applies
before it schedules anything, and the abort names every offender rather than the
first.

Every run here drives a real :class:`Orchestrator` over a real on-disk pack with an
in-memory runtime; the conductor is the repo's own :class:`InMemoryConductor`, whose
call log is what "before any trial" is measured against.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import orchestrator as orchestrator_module
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import (
    EvaluationConfig,
    GradingFindingSeverity,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.unit


def _constraint(match: dict[str, Any]) -> dict[str, Any]:
    """One trace constraint over *match*, weighted — a pack configuring a component
    and declaring no weight for it is refused by the gate on its own account, which
    would make every row here fail for a reason it is not about."""
    return {
        "combine": {"weights": {"trace_checks": 1.0}},
        "trace_checks": {
            "constraints": [
                {
                    "id": "the_agent_called_the_tool",
                    "description": "the agent called the tool",
                    "require": {"present": {"match": match}},
                }
            ]
        },
    }


def _tool_call(tool: str, **args: dict[str, Any]) -> dict[str, Any]:
    match: dict[str, Any] = {"kind": "tool_call", "tool": {"equals": tool}}
    if args:
        match["args"] = args
    return match


# A tool the task declares, so the block is clean and the run proceeds.
CLEAN = _constraint(_tool_call("http_request"))

# ``http_reqest`` is declared by nothing, and ``http_request``'s schema forbids
# extras, so both are errors no run-config setting can downgrade.
UNDECLARED_TOOL = _constraint(_tool_call("http_reqest"))

# An MCP schema declares its properties and permits others, so an unknown argument
# name on one is a probable typo rather than a certainty — the advisory class.
UNKNOWN_MCP_ARGUMENT = _constraint(_tool_call("add_note", titel={"exists": True}))

# ``json``'s own schema declares no properties, so nothing below it is answerable.
UNCHECKABLE_ARGUMENT_PATH = _constraint(_tool_call("http_request", **{"json.q": {"len_gt": 0}}))

# The removed state-check key #696 was filed for: rejected while artifacts are
# written today, which is after the trial has been paid for.
REMOVED_STATE_CHECK_KEY = {"state_checks": {"env_assertions": [{"path": "/etc/hosts"}]}}

# A state hash whose source is a golden replay, so the pack grades only against a world
# its ``task.yaml`` supplies. The action names the one tool the task declares, so what a
# rejection below is about is the world and never the name.
A_GOLDEN_REPLAY = {
    "combine": {"weights": {"state_checks": 1.0}},
    "state_checks": {"hash": {"enabled": True, "golden_actions": [{"name": "http_request"}]}},
}

_MCP_TOOLS_FIXTURE = [
    {
        "name": "add_note",
        "description": "Add a note",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["title", "body"],
        },
    }
]


def _write_builtin_task(root: Path, task_id: str, grading: dict[str, Any]) -> None:
    """A task whose one tool is a builtin — a closed schema the gate can read."""
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    _write_task_yaml(task_dir, task_id, {"agent": {"enabled": ["http_request"]}}, grading)


def _write_mcp_task(root: Path, task_id: str, grading: dict[str, Any]) -> None:
    """A task whose tools come from a committed ``fixtures/tools.json``.

    The fixture is what makes the schema open: an MCP schema declares its
    properties and never declares ``additionalProperties: false``.
    """
    task_dir = root / "tasks" / task_id
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "server.py").write_text("")
    (task_dir / "fixtures" / "tools.json").write_text(json.dumps(_MCP_TOOLS_FIXTURE))
    _write_task_yaml(
        task_dir,
        task_id,
        {"agent": {"enabled": ["add_note"], "mcp_server": "server.py"}},
        grading,
    )


def _write_replaying_task(root: Path, task_id: str, *, mcp_server: str | None) -> None:
    """A task whose state hash is a golden replay, and the world it gives the replay.

    ``initial_state.json`` is written for real, so the only fact such a pack can withhold
    here is *mcp_server* — the one shape the repository's corpus cannot supply, since every
    pack that replays a golden path declares both facts. The declared tool is a builtin
    either way, so the pack naming a module commits the ``fixtures/tools.json`` that keeps
    the description build from spawning it.
    """
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text(json.dumps({"widgets": []}))
    agent: dict[str, Any] = {"enabled": ["http_request"]}
    if mcp_server is not None:
        (task_dir / mcp_server).write_text("")
        (task_dir / "fixtures").mkdir()
        (task_dir / "fixtures" / "tools.json").write_text(json.dumps(_MCP_TOOLS_FIXTURE))
        agent["mcp_server"] = mcp_server
    _write_task_yaml(
        task_dir, task_id, {"agent": agent}, A_GOLDEN_REPLAY, initial_state="initial_state.json"
    )


def _write_gradeless_task(root: Path, task_id: str) -> None:
    """A task naming no grading source at all: no ``grading:`` key, no sibling file.

    Nothing else about the pack is unusual — its one tool is the builtin every clean
    pack here declares — so the only thing the gate can refuse it for is the absence.
    """
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": task_id,
                "description": f"pack {task_id}",
                "tools": {"agent": {"enabled": ["http_request"]}},
            }
        )
    )


def _write_task_yaml(
    task_dir: Path,
    task_id: str,
    tools: dict[str, Any],
    grading: dict[str, Any],
    *,
    initial_state: str | None = None,
) -> None:
    """*initial_state* left unwritten declares no ``initial_state`` block at all."""
    task: dict[str, Any] = {
        "task_id": task_id,
        "description": f"pack {task_id}",
        "tools": tools,
        "grading": "grading.yaml",
    }
    if initial_state is not None:
        task["initial_state"] = {"json_db": initial_state}
    (task_dir / "task.yaml").write_text(yaml.safe_dump(task))
    (task_dir / "grading.yaml").write_text(yaml.safe_dump(grading))


def _orchestrator(
    root: Path,
    output_dir: Path,
    *,
    repeats: int = 1,
    fail_on: GradingFindingSeverity | None = None,
) -> tuple[Orchestrator, InMemoryConductor]:
    """A real orchestrator over *root*, and the conductor recording its trials.

    *fail_on* left at ``None`` writes no ``grading_validation`` block at all, so
    the run reads whatever :class:`GradingValidationConfig` defaults to.

    A judge model is configured so the missing-judge gate returns early: it walks
    every task off-cache when it has to answer, and that walk would otherwise be
    counted against the pre-flight it runs beside.
    """
    evaluation: dict[str, Any] = {"output_dir": str(output_dir), "projects": [str(root)]}
    if fail_on is not None:
        evaluation["grading_validation"] = {"fail_on": fail_on.value}
    conductor = InMemoryConductor()
    orchestrator = Orchestrator(
        RunConfig(
            models={
                "agent": ModelConfig(provider="openai", name="gpt-4"),
                "judge": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
            },
            orchestrator=OrchestratorConfig(
                workers=1, repeats=repeats, auto_start_services=False, shuffle_trials=False
            ),
            evaluation=EvaluationConfig(**evaluation),
        ),
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: conductor,
        ),
    )
    return orchestrator, conductor


# ---------------------------------------------------------------------------
# The gate fires before the first trial, and names every offender
# ---------------------------------------------------------------------------


def test_a_typod_tool_name_aborts_the_run_before_any_trial(tmp_path: Path) -> None:
    """Placement is the whole issue: raising late costs the run it was meant to save.

    Asserted as zero conductor invocations rather than as "an exception was
    raised" — a gate below the scheduling loop still raises.
    """
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-TYPO", UNDECLARED_TOOL)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError) as excinfo:
        orchestrator.run()

    assert conductor.call_log.runs == []
    message = str(excinfo.value)
    assert "TASK-TYPO" in message, message
    assert "http_reqest" in message, message


def test_a_golden_replay_with_no_world_aborts_the_run_before_any_trial(tmp_path: Path) -> None:
    """The pre-flight is handed the task, not only the block it grades by.

    The two facts a golden replay is executed against are written in ``task.yaml`` and
    unreadable from ``grading.yaml``, so the run resolves them for the gate. Resolve
    nothing and the pack below is accepted here, then raises inside the grading engine
    with the trial already paid for. The sibling declaring the module is the control: a
    gate that refused every replaying pack would fail this run identically without it.
    """
    root = tmp_path / "pack"
    _write_replaying_task(root, "TASK-NO-SERVER-MODULE", mcp_server=None)
    _write_replaying_task(root, "TASK-A-COMPLETE-WORLD", mcp_server="mcp_server.py")
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError) as excinfo:
        orchestrator.run()

    assert conductor.call_log.runs == []
    message = str(excinfo.value)
    assert "TASK-NO-SERVER-MODULE" in message, message
    assert "tools.agent.mcp_server" in message, message
    assert "TASK-A-COMPLETE-WORLD" not in message, message


def test_every_offending_task_is_named_in_one_raise(tmp_path: Path) -> None:
    """An author fixing a run's packs wants the list, not the first entry."""
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-A-TYPO", UNDECLARED_TOOL)
    _write_builtin_task(root, "TASK-B-CLEAN", CLEAN)
    _write_builtin_task(root, "TASK-C-REMOVED-KEY", REMOVED_STATE_CHECK_KEY)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError) as excinfo:
        orchestrator.run()

    message = str(excinfo.value)
    assert "TASK-A-TYPO" in message, message
    assert "TASK-C-REMOVED-KEY" in message, message
    assert "TASK-B-CLEAN" not in message, message
    assert conductor.call_log.runs == []


def test_an_offender_selected_last_still_aborts_the_run(tmp_path: Path) -> None:
    """Boundary case: the pass must not short-circuit on an early success."""
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-A-CLEAN", CLEAN)
    _write_builtin_task(root, "TASK-Z-TYPO", UNDECLARED_TOOL)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError, match="TASK-Z-TYPO"):
        orchestrator.run()

    assert conductor.call_log.runs == []


def test_a_removed_grading_key_aborts_before_the_first_trial(tmp_path: Path) -> None:
    """#696: the migration rejections fire at the pre-flight, not while writing
    artifacts — which is the last phase of a trial that has already been paid for.
    """
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-REMOVED-KEY", REMOVED_STATE_CHECK_KEY)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError) as excinfo:
        orchestrator.run()

    assert conductor.call_log.runs == []
    assert "env_assertions" in str(excinfo.value)


def test_prepare_run_rejects_the_pack_before_enqueueing_anything(tmp_path: Path) -> None:
    """A distributed enqueue is rejected once here rather than by every worker."""
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-TYPO", UNDECLARED_TOOL)
    output_dir = tmp_path / "results" / "run_prepared"
    orchestrator, _conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError, match="TASK-TYPO"):
        orchestrator.prepare_run(output_dir)

    assert not (output_dir / "run_queue.sqlite").exists()


def test_a_task_declaring_no_grading_source_aborts_before_the_first_trial(tmp_path: Path) -> None:
    """A pack the adapter cannot grade at all is refused where every other
    ungradeable pack is.

    ``get_grading_config`` raises on such a task while artifacts are written — the
    last phase of a trial whose tokens are already spent — and the gate used to wave
    it through on the grounds that a task with no grading block has no block to
    check. The clean sibling is the control: a gate that refused every pack would
    abort this run identically without it.
    """
    root = tmp_path / "pack"
    _write_gradeless_task(root, "TASK-NO-GRADING-SOURCE")
    _write_builtin_task(root, "TASK-B-CLEAN", CLEAN)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError) as excinfo:
        orchestrator.run()

    assert conductor.call_log.runs == []
    message = str(excinfo.value)
    assert "TASK-NO-GRADING-SOURCE" in message, message
    assert "`grading:`" in message, message
    assert "TASK-B-CLEAN" not in message, message


def test_prepare_run_rejects_a_task_declaring_no_grading_source(tmp_path: Path) -> None:
    """The shape the gate's own reason for existing names: every worker of a
    distributed run would otherwise die identically at grade time."""
    root = tmp_path / "pack"
    _write_gradeless_task(root, "TASK-NO-GRADING-SOURCE")
    output_dir = tmp_path / "results" / "run_prepared"
    orchestrator, _conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(ValueError, match="TASK-NO-GRADING-SOURCE"):
        orchestrator.prepare_run(output_dir)

    assert not (output_dir / "run_queue.sqlite").exists()


# ---------------------------------------------------------------------------
# Which findings are fatal — six cells, all six driven
# ---------------------------------------------------------------------------


_ERROR = GradingFindingSeverity.ERROR

_SEVERITY_CELLS = (
    pytest.param(_write_builtin_task, UNDECLARED_TOOL, None, True, id="error_fail_on_default"),
    pytest.param(_write_builtin_task, UNDECLARED_TOOL, _ERROR, True, id="error_fail_on_error"),
    pytest.param(_write_mcp_task, UNKNOWN_MCP_ARGUMENT, None, True, id="advisory_fail_on_default"),
    pytest.param(_write_mcp_task, UNKNOWN_MCP_ARGUMENT, _ERROR, False, id="advisory_fail_on_error"),
    pytest.param(
        _write_builtin_task, UNCHECKABLE_ARGUMENT_PATH, None, False, id="unchecked_fail_on_default"
    ),
    pytest.param(
        _write_builtin_task, UNCHECKABLE_ARGUMENT_PATH, _ERROR, False, id="unchecked_fail_on_error"
    ),
)


@pytest.mark.parametrize(("write_task", "grading", "fail_on", "aborts"), _SEVERITY_CELLS)
def test_the_config_reaches_advisories_and_nothing_else(
    tmp_path: Path,
    write_task: Any,
    grading: dict[str, Any],
    fail_on: GradingFindingSeverity | None,
    aborts: bool,
) -> None:
    """``fail_on: error`` suppresses only what the schema cannot prove wrong.

    An error stays fatal under either setting — a run that graded a pack whose
    matcher selects nothing would report the author's typo as the agent's failure.
    An ``unchecked`` entry is fatal under neither: it is a channel, not a third
    severity, so the gate has no false-reject mode.
    """
    root = tmp_path / "pack"
    write_task(root, "TASK-UNDER-TEST", grading)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results", fail_on=fail_on)
    if fail_on is None:
        assert orchestrator.config.evaluation.grading_validation.fail_on is (
            GradingFindingSeverity.ADVISORY
        )

    if not aborts:
        orchestrator.run()
        assert len(conductor.call_log.runs) == 1
        return

    with pytest.raises(ValueError, match="TASK-UNDER-TEST"):
        orchestrator.run()
    assert conductor.call_log.runs == []


_REJECTED_SEVERITY_POLICIES = (
    pytest.param({"fail_on": "unchecked"}, "fail_on", id="a_channel_is_not_a_severity"),
    pytest.param({"fail_on": "warning"}, "fail_on", id="a_severity_the_gate_never_reports"),
    pytest.param({"advisory": True}, "advisory", id="a_field_the_block_does_not_carry"),
)


@pytest.mark.parametrize(("block", "named"), _REJECTED_SEVERITY_POLICIES)
def test_a_severity_policy_outside_the_vocabulary_fails_the_config(
    tmp_path: Path, block: dict[str, Any], named: str
) -> None:
    """``fail_on`` is a closed vocabulary, and the block forbids what it does not declare.

    A free-string severity would take ``unchecked`` without a word and then behave
    as the default, silently enforcing the class the operator wrote the key to
    stop enforcing.
    """
    with pytest.raises(ValueError, match=named):
        EvaluationConfig(output_dir=str(tmp_path), grading_validation=block)


def test_a_harness_bug_in_the_gate_is_not_reported_as_the_packs_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offender line sends an author to read the file it names.

    Catching every exception makes the harness miscalling itself indistinguishable
    from a mis-authored pack, and the author reads a file that is fine. No input
    produces a ``TypeError`` here, so it is injected at the seam the orchestrator
    calls; the authoring classes beside it keep their named lines.
    """
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-CLEAN", CLEAN)

    def raise_a_harness_bug(*args: Any, **kwargs: Any) -> None:
        raise TypeError("validate_grading_yaml() got an unexpected keyword argument")

    monkeypatch.setattr(orchestrator_module, "validate_grading_yaml", raise_a_harness_bug)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        orchestrator.run()
    assert conductor.call_log.runs == []


def test_a_grading_file_that_is_not_yaml_stops_the_pass_where_it_stands(
    tmp_path: Path,
) -> None:
    """The every-offender list is of packs that load and cannot be graded.

    Boundary case, standing lock: the native adapter parses ``grading.yaml`` while
    it builds the description, which the pass resolves before the grading
    predicate runs — so an unparseable file surfaces as its own parser error
    naming the file, and the tasks behind it are never read. Widening the pass to
    aggregate it would mean resolving descriptions inside the per-task catch,
    where the adapter-registration guard also lives.
    """
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-UNPARSEABLE", CLEAN)
    _write_builtin_task(root, "TASK-TYPO", UNDECLARED_TOOL)
    (root / "tasks" / "TASK-UNPARSEABLE" / "grading.yaml").write_text("combine: [unclosed\n")
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with pytest.raises(yaml.YAMLError) as excinfo:
        orchestrator.run()

    assert "TASK-UNPARSEABLE" in str(excinfo.value)
    assert "TASK-TYPO" not in str(excinfo.value)
    assert conductor.call_log.runs == []


def test_what_the_gate_could_not_check_is_logged_beside_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A gate that checked nothing must not read as a clean bill of health."""
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-UNCHECKABLE", UNCHECKABLE_ARGUMENT_PATH)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")

    with caplog.at_level(logging.WARNING):
        orchestrator.run()

    warned = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [
        (record.task_id, record.where)
        for record in warned
        if "could not check" in record.getMessage()
    ] == [("TASK-UNCHECKABLE", "trace_checks.the_agent_called_the_tool.present.match.args.json.q")]
    assert "first segment only" in warned[0].reason
    assert len(conductor.call_log.runs) == 1


class _NativeAdapterResolvingToAnExternalType(NativeAdapter):
    """A real native adapter whose descriptions resolve to an external adapter type.

    The gate discriminates on the type the description carries, and
    ``NativeAdapter.to_task_description`` hardcodes ``native`` — so nothing the native
    loader reads can reach the non-native arm on its own. ``terminal_bench`` is a
    registered adapter, so the description still passes the registration guard the
    gate resolves through.
    """

    def to_task_description(self, task_id: str) -> TaskDescription:
        description = super().to_task_description(task_id)
        return description.model_copy(update={"adapter_type": "terminal_bench"})


def test_a_gradeless_pack_an_adapter_answers_for_itself_reaches_its_trials(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the adapter that grades from a file is owed one.

    An adapter resolving its own grading config makes the same absence unanswerable
    rather than a defect, so the gate reports it through ``unchecked`` and the run
    proceeds. Both halves are the lock: passing the pack silently would read as a
    clean bill of health, and refusing it would fail a run over an authoring claim
    nothing here can make.
    """
    root = tmp_path / "pack"
    _write_gradeless_task(root, "TASK-NO-GRADING-SOURCE")
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")
    orchestrator.adapter = _NativeAdapterResolvingToAnExternalType(
        {"task_packs": [str(root)], "tasks_glob": "**/task.yaml"}
    )

    with caplog.at_level(logging.WARNING):
        orchestrator.run()

    warned = [record for record in caplog.records if "could not check" in record.getMessage()]
    assert [(record.task_id, record.where) for record in warned] == [
        ("TASK-NO-GRADING-SOURCE", "grading")
    ]
    assert "'terminal_bench'" in warned[0].reason
    assert len(conductor.call_log.runs) == 1


# ---------------------------------------------------------------------------
# The gate goes through the description builder, not around it
# ---------------------------------------------------------------------------


class _NativeAdapterDeclaringAnUninstalledBackend(NativeAdapter):
    """A real native adapter whose descriptions name a backend the host lacks.

    The registry is open and discovered at runtime, so ``adapter_type`` is a plain
    string on the wire and only the host can say whether it resolves.
    """

    def to_task_description(self, task_id: str) -> TaskDescription:
        description = super().to_task_description(task_id)
        return description.model_copy(update={"adapter_type": "not_installed_here"})


def test_an_unregistered_adapter_type_aborts_at_the_preflight(tmp_path: Path) -> None:
    """The pre-flight resolves descriptions through the orchestrator's own builder.

    Writing ``_task_desc_cache`` directly would be cheaper and would skip the
    registration guard, which only runs on the build — the run would then reach its
    first trial carrying a backend nothing can execute.
    """
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-CLEAN", CLEAN)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results")
    orchestrator.adapter = _NativeAdapterDeclaringAnUninstalledBackend(
        {"task_packs": [str(root)], "tasks_glob": "**/task.yaml"}
    )

    with pytest.raises(ValueError, match="not_installed_here"):
        orchestrator.run()

    assert conductor.call_log.runs == []


def test_the_clean_run_reaches_its_trials_with_every_description_already_built(
    tmp_path: Path,
) -> None:
    """The pre-flight pays for the description build the run already owed.

    Building the descriptions without storing them would leave the cache empty at
    first-trial time and the run would do the work twice.
    """
    root = tmp_path / "pack"
    _write_builtin_task(root, "TASK-A", CLEAN)
    _write_builtin_task(root, "TASK-B", CLEAN)
    orchestrator, conductor = _orchestrator(root, tmp_path / "results", repeats=2)
    orchestrator.load_tasks()

    built: list[str] = []
    cached_at_each_spec: list[frozenset[str]] = []
    resolve = orchestrator.adapter.to_task_description
    build_spec = orchestrator._build_trial_spec

    def recording_resolve(task_id: str) -> TaskDescription:
        built.append(task_id)
        return resolve(task_id)

    def recording_build_spec(**kwargs: Any) -> Any:
        cached_at_each_spec.append(frozenset(orchestrator._task_desc_cache))
        return build_spec(**kwargs)

    orchestrator.adapter.to_task_description = recording_resolve
    orchestrator._build_trial_spec = recording_build_spec

    orchestrator.run()

    assert len(conductor.call_log.runs) == 4
    assert sorted(built) == ["TASK-A", "TASK-B"]
    assert cached_at_each_spec[0] == frozenset({"TASK-A", "TASK-B"})
