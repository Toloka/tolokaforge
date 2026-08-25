"""``build_grade_request_fields`` — the client-side snapshot builder.

Locks the projection from a completed trial's :class:`TrialSpec` into the
wire fields :meth:`GraderServiceImpl.Grade` v2 consumes. The builder reads
only ``spec.task`` and ``spec.judge_model_config``, never opens a gRPC
channel, never touches the filesystem — every field is either a
``.model_dump_json()`` on an in-memory Pydantic model or a plain-string
passthrough.

This test surfaces two shapes of regression the composite dispatch
depends on catching early: a wire field that silently under-populates
(missing round-trip of ``id_fields`` / ``unstable_fields`` /
``tool_artifacts`` on the ``task_description_json``), or a judge-config
field that lands as non-empty when the trial's
``spec.judge_model_config`` is unset (the grader would then construct a
judge for a task that declared none).
"""

from __future__ import annotations

import json

import pytest

from tests.canonical._factories import make_task_description, make_trial_spec
from tolokaforge.core.models import ModelConfig
from tolokaforge.grader.wire_snapshot import GradeRequestFields, build_grade_request_fields
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    RunnerStateChecksConfig,
    UnstableFieldSpec,
)

pytestmark = pytest.mark.unit


def _judge_config() -> LLMJudgeConfig:
    return LLMJudgeConfig(
        rubric=Rubric(
            criteria=[
                Criterion(
                    id="answer_present",
                    description="agent produced an answer",
                    weight=1.0,
                ),
            ],
        ),
    )


def _grading_config(
    *,
    state_checks: RunnerStateChecksConfig | None = None,
    llm_judge: LLMJudgeConfig | None = None,
) -> RunnerGradingConfig:
    return RunnerGradingConfig(
        weights={"llm_judge": 1.0},
        state_checks=state_checks,
        llm_judge=llm_judge,
    )


def _spec_with(
    *,
    grading: RunnerGradingConfig,
    initial_state: RunnerInitialStateConfig | None = None,
    tool_artifacts: dict[str, str] | None = None,
    judge_model_config: ModelConfig | None = None,
):
    task = make_task_description()
    task = task.model_copy(
        update={
            "grading": grading,
            "initial_state": initial_state or task.initial_state,
            "tool_artifacts": tool_artifacts or {},
        }
    )
    spec = make_trial_spec()
    return spec.model_copy(update={"task": task, "judge_model_config": judge_model_config})


class TestReturnShape:
    def test_returns_frozen_dataclass_with_the_five_wire_fields(self) -> None:
        spec = _spec_with(grading=_grading_config())
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="You are the agent.",
            runner_substrate_address="runner:50051",
        )
        assert isinstance(result, GradeRequestFields)
        # ``frozen=True`` — a downstream caller that mutates a field would
        # skew the wire vs. the packed job under queue transport. Dataclass
        # raises ``FrozenInstanceError`` (a ``AttributeError`` subclass).
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            result.task_config_json = "mutated"  # type: ignore[misc]


class TestFieldDerivation:
    def test_task_config_json_is_grading_model_dump_json(self) -> None:
        grading = _grading_config()
        spec = _spec_with(grading=grading)
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="",
        )
        assert result.task_config_json == grading.model_dump_json()
        # The whole grading block round-trips via JSON with no field
        # coerced away — an operator reading ``weights`` on the grader
        # side must see what the client packed.
        parsed = json.loads(result.task_config_json)
        assert parsed["weights"] == {"llm_judge": 1.0}

    def test_task_description_json_round_trips_id_fields(self) -> None:
        """The three fields the earlier plan draft carried as separate wire
        entries (id_fields, unstable_fields, tool_artifacts) all reach the
        grader through ``task_description_json``. Round-trip locks the
        promise the plan made when it collapsed them into one field."""
        id_fields = {"widgets": "widget_id", "positions": ["account_id", "symbol"]}
        state_checks = RunnerStateChecksConfig(id_fields=id_fields)
        spec = _spec_with(grading=_grading_config(state_checks=state_checks))
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="",
        )
        parsed = json.loads(result.task_description_json)
        assert parsed["grading"]["state_checks"]["id_fields"] == id_fields

    def test_task_description_json_round_trips_unstable_fields(self) -> None:
        initial_state = RunnerInitialStateConfig(
            unstable_fields=[
                UnstableFieldSpec(table_name="tickets", field_name="id", reason="auto_id"),
                UnstableFieldSpec(
                    table_name="tickets", field_name="created_at", reason="timestamp"
                ),
            ],
        )
        spec = _spec_with(grading=_grading_config(), initial_state=initial_state)
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="",
        )
        parsed = json.loads(result.task_description_json)
        got = {
            (u["table_name"], u["field_name"]) for u in parsed["initial_state"]["unstable_fields"]
        }
        assert got == {("tickets", "id"), ("tickets", "created_at")}

    def test_task_description_json_round_trips_tool_artifacts(self) -> None:
        artifacts = {
            "checks.py": "cHJpbnQoImhlbGxvIikK",  # base64: print("hello")\n
            "tools/helper.py": "IyBoZWxwZXIK",  # base64: # helper\n
        }
        spec = _spec_with(grading=_grading_config(), tool_artifacts=artifacts)
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="",
        )
        parsed = json.loads(result.task_description_json)
        assert parsed["tool_artifacts"] == artifacts


class TestJudgeModelConfig:
    def test_judge_model_config_json_is_empty_when_spec_carries_none(self) -> None:
        spec = _spec_with(grading=_grading_config(), judge_model_config=None)
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="",
        )
        assert result.judge_model_config_json == ""

    def test_judge_model_config_json_is_model_dump_json_when_set(self) -> None:
        judge = ModelConfig(provider="litellm", name="openrouter/openai/gpt-5")
        spec = _spec_with(
            grading=_grading_config(llm_judge=_judge_config()),
            judge_model_config=judge,
        )
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="",
        )
        assert result.judge_model_config_json == judge.model_dump_json()


class TestPassthroughs:
    def test_agent_system_prompt_is_verbatim(self) -> None:
        spec = _spec_with(grading=_grading_config())
        prompt = "You are the test agent.\nFollow the policy."
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt=prompt,
            runner_substrate_address="",
        )
        assert result.agent_system_prompt == prompt

    def test_runner_substrate_address_is_verbatim(self) -> None:
        spec = _spec_with(grading=_grading_config())
        result = build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="runner.grid-01:50051",
        )
        assert result.runner_substrate_address == "runner.grid-01:50051"


class TestNoSideEffects:
    def test_never_opens_a_grpc_channel_or_touches_the_filesystem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Side-effect probes: any ``grpc.insecure_channel`` or filesystem
        open under the CWD would fire during the call. The builder must
        stay a pure projection over the in-memory Pydantic tree."""
        import grpc

        def _fail_grpc(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("build_grade_request_fields opened a gRPC channel")

        monkeypatch.setattr(grpc, "insecure_channel", _fail_grpc)

        real_open = open

        def _guard_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            path = str(file)
            if path.endswith(".py") or path.endswith(".json"):
                raise AssertionError(f"build_grade_request_fields opened {path!r}")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _guard_open)

        spec = _spec_with(
            grading=_grading_config(llm_judge=_judge_config()),
            judge_model_config=ModelConfig(provider="litellm", name="openrouter/openai/gpt-5"),
            tool_artifacts={"checks.py": "aGVsbG8="},
        )
        # Should not raise — the builder never touches gRPC or filesystem.
        build_grade_request_fields(
            spec=spec,
            agent_system_prompt="",
            runner_substrate_address="runner:50051",
        )
