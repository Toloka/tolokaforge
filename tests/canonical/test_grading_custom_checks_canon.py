"""Pin custom_checks routing through the runner-side grading path.

Locks four behaviours that a future refactor of the grade pipeline must
not silently regress:

1. :func:`combine_grade_components` includes the ``custom_checks`` score
   as a weighted contributor when it is present (``>= 0``).
2. A custom-checks-only pack whose executor produced no score falls into
   the empty-active-components guard — returns ``(0.0, False)``, NOT the
   ``(1.0, True)`` silent-pass the pre-Stage-2 runner emitted. This is
   the exact regression this stage exists to close (AGENTS.md Rule 1 —
   surface failures, don't drop them).
3. :func:`_parse_grade_result` decodes the wire ``custom_checks`` list
   into :class:`~tolokaforge.core.models.CustomCheckDetail` on the host
   :class:`~tolokaforge.core.models.Grade`, including the
   ``details_json`` → dict round-trip.
4. ``RunnerService.RegisterTrial`` fails-loud on an unsupported
   ``interface_version`` (or a ``checks.py`` that fails to load) BEFORE
   the trial runs — the actionable error names both the offending
   version and ``SUPPORTED_VERSIONS``. A supported version registers
   cleanly; a pack with ``enabled: false`` skips validation entirely.
"""

from __future__ import annotations

import base64
import json

import pytest

from tolokaforge.core.grading.checks_interface import SUPPORTED_VERSIONS
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.core.trial_grader import _parse_grade_result
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.grading import combine_grade_components
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.canonical, pytest.mark.grading]


class TestCombineGradeComponentsRoutesCustomChecks:
    """The custom_checks score participates in the weighted combine just
    like the other components — added to ``active_components`` when
    ``>= 0`` and weighted per ``combine.weights``.
    """

    def test_custom_checks_score_is_a_weighted_contributor(self) -> None:
        """state_checks=1.0 (w .5) + custom_checks=0.4 (w .5) -> 0.7."""
        components = {
            "hash_score": 1.0,
            "custom_checks_score": 0.4,
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {"state_checks": 0.5, "custom_checks": 0.5},
            "pass_threshold": 0.75,
            "custom_checks": {"enabled": True, "file": "checks.py"},
            "state_checks": {"jsonpaths": []},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == pytest.approx(0.7)
        assert binary_pass is False

    def test_custom_checks_only_pack_passes_when_score_meets_threshold(self) -> None:
        """A pack with only custom_checks configured passes when the score clears the bar."""
        components = {"custom_checks_score": 1.0}
        grading_config = {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
            "custom_checks": {"enabled": True, "file": "checks.py"},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == pytest.approx(1.0)
        assert binary_pass is True


class TestCombineGradeComponentsGuardsAgainstSilentPass:
    """Regression lock for the silent-pass that shipped before Stage 2.

    Pre-Stage-2: a custom-checks-only pack whose score came back absent
    (``-1.0`` — the "Not implemented yet" stub) yielded an empty
    ``active_components`` set. Because ``custom_checks`` was not in the
    fail-loud ``actually_configured`` guard, the function returned
    ``(1.0, True)`` — a false success. Stage 2 closes both halves: the
    score is really computed AND the guard recognises custom_checks as a
    configured component.
    """

    def test_custom_checks_only_config_absent_score_fails_via_guard(self) -> None:
        components: dict = {}  # no custom_checks_score present -> not in active
        grading_config = {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 1.0,
            "custom_checks": {"enabled": True, "file": "checks.py"},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == 0.0
        assert binary_pass is False


class TestParseGradeResultMapsCustomCheckDetails:
    """The wire ``custom_checks`` list decodes into ``Grade.custom_checks_details``."""

    def test_populates_per_check_details_from_raw_list(self) -> None:
        raw_grade = {
            "binary_pass": True,
            "score": 0.75,
            "components": {"custom_checks": 0.75},
            "reasons": "",
            "custom_checks": [
                {
                    "check_name": "workflow_completed",
                    "status": "passed",
                    "score": 1.0,
                    "message": "workflow finished",
                    "details_json": json.dumps({"steps": 3}),
                },
                {
                    "check_name": "final_state_matches",
                    "status": "failed",
                    "score": 0.5,
                    "message": "counter off by one",
                    "details_json": "",
                },
            ],
        }

        parsed = _parse_grade_result(raw_grade)

        assert parsed.custom_checks_details is not None
        assert [d.check_name for d in parsed.custom_checks_details] == [
            "workflow_completed",
            "final_state_matches",
        ]
        assert parsed.custom_checks_details[0].status == "passed"
        assert parsed.custom_checks_details[0].score == 1.0
        assert parsed.custom_checks_details[0].details == {"steps": 3}
        assert parsed.custom_checks_details[1].status == "failed"
        assert parsed.custom_checks_details[1].score == 0.5
        assert parsed.custom_checks_details[1].details is None

    def test_absent_custom_checks_yields_none(self) -> None:
        raw_grade = {
            "binary_pass": True,
            "score": 1.0,
            "components": {},
            "reasons": "",
        }

        parsed = _parse_grade_result(raw_grade)

        assert parsed.custom_checks_details is None

    def test_malformed_details_json_drops_details_but_keeps_check(self) -> None:
        """A malformed ``details_json`` string (bad JSON) drops the details payload
        to ``None`` rather than failing the whole grade parse — the audit of *which*
        check produced the malformed payload is preserved on the entry itself.
        """
        raw_grade = {
            "binary_pass": True,
            "score": 1.0,
            "components": {"custom_checks": 1.0},
            "reasons": "",
            "custom_checks": [
                {
                    "check_name": "produced_bad_json",
                    "status": "passed",
                    "score": 1.0,
                    "message": "",
                    "details_json": "{not-json",
                }
            ],
        }

        parsed = _parse_grade_result(raw_grade)

        assert parsed.custom_checks_details is not None
        assert len(parsed.custom_checks_details) == 1
        assert parsed.custom_checks_details[0].check_name == "produced_bad_json"
        assert parsed.custom_checks_details[0].details is None


# =============================================================================
# RegisterTrial fail-loud on unsupported ``interface_version``
# =============================================================================


_CHECKS_PY_TEMPLATE = """\
from tolokaforge.core.grading.checks_interface import CheckPassed, check, init


@init(interface_version="{version}")
def setup(ctx):
    pass


@check
def dummy():
    return CheckPassed(message="ok")
"""


def _checks_py_artifact(version: str) -> dict[str, str]:
    """Base64-encoded ``checks.py`` artifact declaring ``interface_version``."""
    source = _CHECKS_PY_TEMPLATE.format(version=version).encode("utf-8")
    return {"checks.py": base64.b64encode(source).decode("ascii")}


def _custom_checks_task(
    *,
    enabled: bool,
    interface_version: str,
    artifacts: dict[str, str] | None,
) -> dict:
    """Minimal :class:`TaskDescription` dict wiring ``custom_checks``."""
    return {
        "task_id": "custom_checks_register_test",
        "name": "Custom checks register test",
        "category": "test",
        "description": "Exercises RegisterTrial custom_checks validation.",
        "adapter_type": "tau",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": []},
        "agent_tools": [],
        "user_tools": [],
        "tool_artifacts": artifacts or {},
        "grading": {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
            "custom_checks": {
                "enabled": enabled,
                "file": "checks.py",
                "interface_version": interface_version,
            },
        },
    }


def _trial_spec_json(task_dict: dict, trial_id: str) -> str:
    """Build a valid :class:`TrialSpec` JSON wrapping ``task_dict``."""
    return TrialSpec(
        trial_id=trial_id,
        run_id="canon_run",
        task=TaskDescription.model_validate(task_dict),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


class TestRegisterTrialValidatesCustomChecksInterfaceVersion:
    """Fail-loud contract for the pack-authored ``interface_version``.

    Moves the version check from the load-time ``ValueError`` at trial end
    (``check_runner.load_checks_module``) to trial startup: an unsupported
    version rejects the registration BEFORE the (expensive) agent loop
    runs. The pre-existing runtime error remains as defense-in-depth for
    edge cases the startup validator might miss.
    """

    def test_unsupported_version_rejects_at_register_with_actionable_message(
        self, runner_service, mock_grpc_context
    ) -> None:
        trial_id = "custom_checks_unsupported:0"
        task = _custom_checks_task(
            enabled=True,
            interface_version="9.9",
            artifacts=_checks_py_artifact("9.9"),
        )
        request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            trial_spec_json=_trial_spec_json(task, trial_id=trial_id),
        )

        response = runner_service.RegisterTrial(request, mock_grpc_context)

        assert response.success is False
        assert "9.9" in response.error
        assert str(SUPPORTED_VERSIONS) in response.error
        assert trial_id not in runner_service.trials

    def test_supported_version_registers_cleanly(self, runner_service, mock_grpc_context) -> None:
        trial_id = "custom_checks_supported:0"
        task = _custom_checks_task(
            enabled=True,
            interface_version="1.0",
            artifacts=_checks_py_artifact("1.0"),
        )
        request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            trial_spec_json=_trial_spec_json(task, trial_id=trial_id),
        )

        response = runner_service.RegisterTrial(request, mock_grpc_context)

        assert response.success is True, f"Registration failed: {response.error}"
        assert response.error == ""
        assert trial_id in runner_service.trials

    def test_disabled_custom_checks_skips_validation(
        self, runner_service, mock_grpc_context
    ) -> None:
        """``enabled: false`` short-circuits validation — a ``checks.py`` that
        would otherwise reject at startup (unsupported ``9.9``) does not
        prevent registration when the pack has opted out.
        """
        trial_id = "custom_checks_disabled:0"
        task = _custom_checks_task(
            enabled=False,
            interface_version="9.9",
            artifacts=_checks_py_artifact("9.9"),
        )
        request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            trial_spec_json=_trial_spec_json(task, trial_id=trial_id),
        )

        response = runner_service.RegisterTrial(request, mock_grpc_context)

        assert response.success is True, f"Registration failed: {response.error}"
        assert trial_id in runner_service.trials

    def test_enabled_without_delivered_checks_file_rejects(
        self, runner_service, mock_grpc_context
    ) -> None:
        """``enabled: true`` with no ``tool_artifacts`` is a delivery bug that
        must surface at startup — no silent-pass at trial end.
        """
        trial_id = "custom_checks_missing_delivery:0"
        task = _custom_checks_task(
            enabled=True,
            interface_version="1.0",
            artifacts=None,
        )
        request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            trial_spec_json=_trial_spec_json(task, trial_id=trial_id),
        )

        response = runner_service.RegisterTrial(request, mock_grpc_context)

        assert response.success is False
        assert "checks.py" in response.error or "custom_checks" in response.error
        assert trial_id not in runner_service.trials
