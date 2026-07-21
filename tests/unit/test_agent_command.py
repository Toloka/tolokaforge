"""Pure-function contract for ``tolokaforge agent``: envelope parse, error
marshal, result marshal, and command registration — no subprocess, no services.

The subprocess behaviour lock (canned stdin → expected stdout) lives in the
canonical tier (``tests/canonical/test_agent_subprocess.py``).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from tests.canonical._factories import make_trajectory
from tolokaforge.cli.agent_command import (
    CancelMessage,
    ProtocolError,
    StartMessage,
    _Cancelled,
    marshal_error,
    marshal_result,
    parse_envelope,
)
from tolokaforge.core.plugin_registry import (
    RUNTIME_BACKENDS_GROUP,
    UnknownImplementationError,
)
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import TrialResult

pytestmark = pytest.mark.unit


class TestParseEnvelope:
    def test_well_formed_start_populates_start_message(self) -> None:
        line = (
            '{"v":1,"type":"start","task":{"task_id":"t"},"models":{"agent":{}},'
            '"runtime":"in_memory","grader":"runner_rpc","conductor":"in_memory"}'
        )
        message = parse_envelope(line)
        assert message == StartMessage(
            task={"task_id": "t"},
            models={"agent": {}},
            runtime="in_memory",
            grader="runner_rpc",
            conductor="in_memory",
        )

    def test_start_omitting_seams_defaults_to_run_trial_defaults(self) -> None:
        message = parse_envelope('{"v":1,"type":"start","task":{},"models":{}}')
        assert isinstance(message, StartMessage)
        assert (message.runtime, message.grader, message.conductor) == (
            "auto",
            "runner_rpc",
            "in_process",
        )

    def test_cancel_envelope_parses_to_cancel_message(self) -> None:
        assert parse_envelope('{"v":1,"type":"cancel"}') == CancelMessage()

    def test_malformed_json_raises_protocol_error(self) -> None:
        with pytest.raises(ProtocolError):
            parse_envelope("{not json")

    def test_wrong_version_raises_protocol_error(self) -> None:
        with pytest.raises(ProtocolError):
            parse_envelope('{"v":2,"type":"start","task":{},"models":{}}')

    def test_unknown_type_raises_protocol_error(self) -> None:
        with pytest.raises(ProtocolError):
            parse_envelope('{"v":1,"type":"resume","task":{},"models":{}}')


class TestMarshalError:
    def _synthetic_validation_error(self) -> ValidationError:
        class _M(BaseModel):
            x: int

        try:
            _M.model_validate({"x": "not-an-int"})
        except ValidationError as exc:
            return exc
        raise AssertionError("expected a ValidationError")

    def test_validation_error_maps_to_validation_error(self) -> None:
        envelope = marshal_error(self._synthetic_validation_error())
        assert envelope["error_type"] == "ValidationError"
        assert envelope["fatal"] is True
        assert envelope["v"] == 1
        assert envelope["type"] == "error"

    def test_unknown_implementation_error_lists_known_names(self) -> None:
        exc = UnknownImplementationError("bogus", RUNTIME_BACKENDS_GROUP, ["shared", "in_memory"])
        envelope = marshal_error(exc)
        assert envelope["error_type"] == "UnknownImplementationError"
        assert envelope["fatal"] is True
        assert "shared" in envelope["message"] and "in_memory" in envelope["message"]

    def test_provision_error_maps_to_provision_error(self) -> None:
        exc = ProvisionError(trial_id="t:0", stage="await_ready", reason="stack not ready")
        envelope = marshal_error(exc)
        assert envelope["error_type"] == "ProvisionError"
        assert envelope["fatal"] is True

    def test_cancelled_maps_to_cancelled(self) -> None:
        envelope = marshal_error(_Cancelled("stdin closed before a start message"))
        assert envelope["error_type"] == "cancelled"
        assert envelope["fatal"] is True

    def test_bare_runtime_error_maps_to_internal_error(self) -> None:
        envelope = marshal_error(RuntimeError("boom"))
        assert envelope["error_type"] == "InternalError"
        assert envelope["fatal"] is True


class TestMarshalResult:
    def test_minimal_result_round_trips_via_model_validate(self) -> None:
        result = TrialResult(trial_id="task-1:0", trajectory=make_trajectory())
        envelope = marshal_result(result)
        assert envelope["v"] == 1
        assert envelope["type"] == "result"
        assert TrialResult.model_validate(envelope["result"]) == result


class TestRegistration:
    def test_agent_command_is_registered(self) -> None:
        from tolokaforge.cli.main import cli

        assert "agent" in cli.commands
