"""Real-scenario locks for the runner-side model reconcile.

The runner-side wire schema and the core-side YAML-authoring schema each
own the concerns that share a concept but differ in serialisation shape
(flat vs nested, strict vs lax). Where the two shapes genuinely differ the
runner-side type carries a ``Runner`` prefix, so a single import namespace
never carries two classes with the same name; where one shape serves both
— ``TranscriptRulesConfig`` and its ``RequiredAction`` / ``CommunicateInfo``
elements, as ``TraceChecksConfig`` already did — there is one unprefixed
class, defined in ``runner.models`` and re-exported by the core shim.

These tests exercise the four real user paths that touch the reconciled
types:

- **Persona 1 (gRPC wire consumer)** — a ``TaskDescription`` built by
  the native adapter round-trips through ``model_dump_json()`` /
  ``model_validate_json()`` bit-for-bit, so every nested runner-side
  wire type (``RunnerGradingConfig``, ``RunnerStateChecksConfig``,
  ``TranscriptRulesConfig``, ``RunnerInitialStateConfig``,
  ``RunnerUserSimulatorConfig``, ``RequiredAction``,
  ``RunnerInitializationAction``) survives the wire.
- **Persona 2 (RunnerRPCTrialGrader contract)** — a
  ``RunnerGradingConfig`` carrying a real ``LLMJudgeConfig`` +
  ``JudgeCustomization`` round-trips through ``model_dump()`` /
  ``model_validate()`` unchanged, so the ``GradeTrial`` payload the
  orchestrator constructs still deserialises on the runner side.
- **Persona 3 (name-collision invariant)** — no top-level Pydantic
  ``BaseModel`` class name is declared in both
  ``tolokaforge.core.models`` and ``tolokaforge.runner.models``, so a
  caller reaching either module for a wire type never has to know which
  Python object the name resolves to.
- **Persona 4 (JSON-Lines / adapter output)** — ``NativeAdapter``
  builds a ``TaskDescription`` from a bundled example and every
  nested slot in that description carries the wire-side class, never a
  core-side sibling of the same concern.

The runner-side classes are used through ``tolokaforge.runner.models``;
the core-side siblings are still importable through
``tolokaforge.core.models`` under their unprefixed names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import models as core_models
from tolokaforge.runner import models as runner_models
from tolokaforge.runner.models import (
    CommunicateInfo,
    Criterion,
    JudgeCustomization,
    LLMJudgeConfig,
    RecordedToolCall,
    RequiredAction,
    Rubric,
    RunnerGradeComponents,
    RunnerGradingConfig,
    RunnerInitializationAction,
    RunnerInitialStateConfig,
    RunnerStateChecksConfig,
    RunnerUserSimulatorConfig,
    TaskDescription,
    ToolExpectations,
    TranscriptRulesConfig,
)

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_TOOL_USE = _REPO_ROOT / "examples" / "native" / "tool_use"


# ────────────────────────────────────────────────────────────────
# Persona 1 — gRPC wire consumer: TaskDescription round-trip
# ────────────────────────────────────────────────────────────────


def _sample_task_description() -> TaskDescription:
    """A ``TaskDescription`` that populates every reconciled nested type.

    Mirrors the shape the native adapter builds when a task declares an
    initial state, initialisation actions, grading rubric, state checks,
    transcript rules, and a user simulator.
    """
    rubric = Rubric(
        criteria=[
            Criterion(
                id="answers_the_question",
                description="Reply contains the customer's requested value.",
                weight=1.0,
                kind="binary",
                required=True,
            )
        ]
    )
    return TaskDescription(
        task_id="reconcile:0",
        name="reconcile-fixture",
        category="general",
        description="round-trip fixture",
        adapter_type="native",
        system_prompt="You are a helpful assistant.",
        initial_state=RunnerInitialStateConfig(
            tables={"orders": [{"id": 1, "status": "open"}]},
            filesystem={"/work/README.md": "seed"},
        ),
        initialization_actions=[
            RunnerInitializationAction(
                env_type="assistant",
                tool_name="seed_orders",
                arguments={"count": 1},
            )
        ],
        user_simulator=RunnerUserSimulatorConfig(
            mode="llm",
            persona="cooperative",
            backstory="Wants a refund on order 1.",
            first_message="Please refund order 1.",
            user_context={"customer_id": "c-42"},
        ),
        grading=RunnerGradingConfig(
            combine_method="weighted",
            weights={"state_checks": 0.5, "llm_judge": 0.5},
            pass_threshold=0.6,
            state_checks=RunnerStateChecksConfig(
                hash_enabled=True,
                golden_actions=[],
                hash_weight=1.0,
                jsonpath_checks=[{"path": "$.orders[0].status", "equals": "refunded"}],
            ),
            transcript_rules=TranscriptRulesConfig(
                must_contain=["refund"],
                required_actions=[
                    RequiredAction(
                        action_id="refund_call",
                        requestor="assistant",
                        name="issue_refund",
                        arguments={"order_id": 1},
                    )
                ],
                tool_expectations=ToolExpectations(
                    required_tools=["issue_refund"],
                    disallowed_tools=["escalate_to_manager"],
                ),
            ),
            llm_judge=LLMJudgeConfig(
                rubric=rubric,
                customization=JudgeCustomization(
                    disable_knowledge_search=False,
                ),
            ),
        ),
    )


def test_task_description_round_trips_through_grpc_json_wire():
    """Persona 1 — every reconciled runner-side type survives the
    ``TaskDescription`` JSON wire the runner service consumes.

    ``TrialSpec.trial_spec_json`` is the wire. Locking round-trip on
    ``TaskDescription`` alone covers every nested reconciled class,
    because ``TaskDescription`` is the concrete graph the runner reads.
    """
    task = _sample_task_description()
    wire = task.model_dump_json()
    reloaded = TaskDescription.model_validate_json(wire)

    # Bit-for-bit identical JSON on both sides.
    assert reloaded.model_dump_json() == wire

    # And every reconciled nested slot carries its wire-side class.
    assert isinstance(reloaded.grading, RunnerGradingConfig)
    assert isinstance(reloaded.grading.state_checks, RunnerStateChecksConfig)
    assert isinstance(reloaded.grading.transcript_rules, TranscriptRulesConfig)
    assert isinstance(reloaded.grading.llm_judge, LLMJudgeConfig)
    assert isinstance(reloaded.initial_state, RunnerInitialStateConfig)
    assert isinstance(reloaded.user_simulator, RunnerUserSimulatorConfig)
    assert all(isinstance(a, RunnerInitializationAction) for a in reloaded.initialization_actions)
    tr_rules = reloaded.grading.transcript_rules
    assert tr_rules is not None
    assert all(isinstance(a, RequiredAction) for a in tr_rules.required_actions)


def test_reconciled_wire_types_forbid_extras():
    """The runner-side wire types stay strict — a stray key is a wire
    contract violation and must fail loud, not be silently dropped.

    Locks each class independently (``TaskDescription``'s own forbid rule
    doesn't reach nested siblings by construction).
    """
    from pydantic import ValidationError

    def _requires_forbid(model_cls: type, payload: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            model_cls.model_validate({**payload, "definitely_unknown_key": 1})

    _requires_forbid(RunnerGradingConfig, {})
    _requires_forbid(RunnerStateChecksConfig, {})
    _requires_forbid(TranscriptRulesConfig, {})
    _requires_forbid(RunnerInitialStateConfig, {})
    _requires_forbid(RunnerUserSimulatorConfig, {"mode": "llm", "persona": "cooperative"})
    _requires_forbid(
        RequiredAction,
        {"action_id": "a", "requestor": "assistant", "name": "t"},
    )
    _requires_forbid(CommunicateInfo, {"info": "x"})
    _requires_forbid(
        RunnerInitializationAction,
        {"env_type": "assistant", "tool_name": "t"},
    )
    _requires_forbid(RunnerGradeComponents, {})


# ────────────────────────────────────────────────────────────────
# Persona 2 — RunnerRPCTrialGrader: judge config round-trip
# ────────────────────────────────────────────────────────────────


def test_grading_config_with_judge_round_trips_for_the_rpc_grader():
    """Persona 2 — the orchestrator-built ``GradeTrialRequest`` carries
    a ``RunnerGradingConfig`` with an LLM judge; the runner deserialises
    it via the same class. Locking the ``model_dump()`` /
    ``model_validate()`` idempotence pins the contract the orchestrator
    and runner both hold.
    """
    rubric = Rubric(
        criteria=[
            Criterion(
                id="quotes_correct_refund",
                description="Response quotes the correct refund amount.",
                weight=1.0,
                kind="binary",
                required=True,
                expected="$42.00",
            )
        ],
        reference="Correct refund is $42.00.",
    )
    grading = RunnerGradingConfig(
        combine_method="weighted",
        weights={"llm_judge": 1.0},
        pass_threshold=0.75,
        llm_judge=LLMJudgeConfig(
            rubric=rubric,
            customization=JudgeCustomization(
                disable_knowledge_search=True,
                system_prompt="You are a strict rubric judge.",
                include_agent_system_prompt=False,
            ),
        ),
    )

    dumped = grading.model_dump()
    reconstructed = RunnerGradingConfig.model_validate(dumped)
    assert reconstructed.model_dump() == dumped
    assert reconstructed.llm_judge is not None
    assert reconstructed.llm_judge.rubric.reference == "Correct refund is $42.00."
    assert reconstructed.llm_judge.customization is not None
    assert reconstructed.llm_judge.customization.disable_knowledge_search is True
    assert reconstructed.llm_judge.customization.include_agent_system_prompt is False


def test_recorded_tool_call_is_the_shared_wire_type():
    """The recorded-tool-call vocabulary is the shared wire type both
    grading substrates read (``RunnerRPCTrialGrader`` on the runner,
    core evaluators on the host). The reconcile keeps this canonical
    home in ``runner.models`` and re-exports it via the core shim.
    """
    assert RecordedToolCall is core_models.RecordedToolCall
    assert RecordedToolCall.__module__ == "tolokaforge.runner.models"


# ────────────────────────────────────────────────────────────────
# Persona 3 — no name collisions across the two modules
# ────────────────────────────────────────────────────────────────


def _basemodel_names(module) -> set[str]:
    return {
        name
        for name, obj in vars(module).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj.__module__ == module.__name__
    }


def test_no_top_level_pydantic_class_name_is_defined_in_both_modules():
    """Persona 3 — a caller who imports a wire-type name from either
    ``tolokaforge.core.models`` (the shim) or ``tolokaforge.runner.models``
    never has to reason about which Python class the name resolves to.

    Only classes actually **defined** in the module (``__module__`` match)
    count; re-exports through the shim are the whole point of the
    reconcile and are legitimate on both sides.
    """
    # ``core.models`` is a shim package — collect names from every
    # submodule that owns its own definitions.
    core_defined: set[str] = set()
    for submodule_name in (
        "grade",
        "grade_components",
        "model_config",
        "run_config",
        "task_config",
        "trajectory",
    ):
        submodule = getattr(core_models, submodule_name)
        core_defined |= _basemodel_names(submodule)

    runner_defined = _basemodel_names(runner_models)
    collisions = core_defined & runner_defined
    assert collisions == set(), (
        "Pydantic model class names must not be defined in both "
        f"tolokaforge.core.models and tolokaforge.runner.models: {sorted(collisions)}"
    )


# ────────────────────────────────────────────────────────────────
# Persona 4 — NativeAdapter emits the wire-side classes
# ────────────────────────────────────────────────────────────────


def test_native_adapter_builds_task_description_with_the_wire_side_types(tmp_path):
    """Persona 4 — a real bundled example goes through
    ``NativeAdapter.to_task_description`` and every reconciled slot
    carries its wire-side class. This is the path the orchestrator
    actually runs before every trial.

    The example's rubric-less grading exercises the state-checks +
    transcript-rules + user-simulator + initial-state slots; the
    initialisation-actions list is empty in this example so the
    test asserts type shape rather than instance count for that slot.
    """
    tasks_glob = str(_EXAMPLE_TOOL_USE / "dataset" / "tasks" / "**" / "task.yaml")
    adapter = NativeAdapter({"tasks_glob": tasks_glob})
    task_ids = adapter.get_task_ids()
    assert task_ids, "the bundled tool_use example must declare at least one task"
    task_id = task_ids[0]

    td = adapter.to_task_description(task_id)

    assert isinstance(td, TaskDescription)
    assert isinstance(td.grading, RunnerGradingConfig)
    assert isinstance(td.initial_state, RunnerInitialStateConfig)
    assert isinstance(td.user_simulator, RunnerUserSimulatorConfig)
    if td.grading.state_checks is not None:
        assert isinstance(td.grading.state_checks, RunnerStateChecksConfig)
    if td.grading.transcript_rules is not None:
        assert isinstance(td.grading.transcript_rules, TranscriptRulesConfig)
        for action in td.grading.transcript_rules.required_actions:
            assert isinstance(action, RequiredAction)
    for action in td.initialization_actions:
        assert isinstance(action, RunnerInitializationAction)


def test_task_description_json_wire_shape_uses_flat_wire_field_names():
    """The gRPC / JSON-Lines wire schema is unchanged by the reconcile.

    Field names on the serialised payload are what wire consumers depend
    on. Locking the shape at the ``model_dump()`` level here means a
    future rename that broke the JSON keys (e.g. renaming
    ``hash_enabled`` back to ``hash.enabled``) would fail the test even
    though the Python class carries a new name.
    """
    td = _sample_task_description()
    dumped = json.loads(td.model_dump_json())

    grading = dumped["grading"]
    # Flat combine block (wire-form): the runner-side flat naming stays
    # regardless of the class-name change.
    assert set(grading) >= {
        "combine_method",
        "weights",
        "pass_threshold",
        "state_checks",
        "transcript_rules",
        "llm_judge",
    }
    assert grading["state_checks"] is not None
    assert set(grading["state_checks"]) >= {
        "hash_enabled",
        "golden_actions",
        "hash_weight",
        "jsonpath_checks",
    }
    # Transcript rules keep their wire field names.
    tr = grading["transcript_rules"]
    assert set(tr) >= {"must_contain", "required_actions", "tool_expectations"}
    for action in tr["required_actions"]:
        # One model serves the authored block and the wire, so the wire carries the
        # author's ``name``. The two surfaces are version-locked in both directions:
        # a spec spelling it ``tool_name`` is refused by ``extra="forbid"``.
        assert "name" in action
        assert "tool_name" not in action

    # ``initialization_actions`` keeps its wire ``tool_name``: it is a harness setup
    # instruction rather than a grading assertion about the agent, and its author-side
    # spelling (``func_name``) differs from both.
    for action in dumped["initialization_actions"]:
        assert "tool_name" in action
