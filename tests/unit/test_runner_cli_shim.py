"""Pure-function contract for the subset-native CLI shim (``tolokaforge.runner._cli``):
envelope parse, error marshal, result marshal — no subprocess, no gRPC.

The shim is bound as the subset wheel's ``[project.scripts]`` entry
(``tolokaforge = tolokaforge.runner._cli:main``) per ADR-0027 and mirrors
the base wheel's ADR-0022 § Surface 3 wire framing, so the shape of these
tests parallels ``tests/unit/test_run_trial_command.py``.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from tolokaforge.runner._cli import (
    CancelMessage,
    ProtocolError,
    ProvisionError,
    StartMessage,
    _Cancelled,
    marshal_error,
    marshal_result,
    parse_envelope,
)

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

    def test_start_omitting_seams_defaults_to_library_defaults(self) -> None:
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

    def test_unknown_envelope_key_is_protocol_error(self) -> None:
        with pytest.raises(ProtocolError, match="runtme"):
            parse_envelope('{"v":1,"type":"start","runtme":"x","task":{},"models":{}}')

    def test_unknown_envelope_key_in_cancel_is_protocol_error(self) -> None:
        with pytest.raises(ProtocolError, match="extra"):
            parse_envelope('{"v":1,"type":"cancel","extra":"x"}')

    def test_bare_string_envelope_is_protocol_error(self) -> None:
        with pytest.raises(ProtocolError, match="JSON object"):
            parse_envelope('"just a string"')


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

    def test_provision_error_maps_to_provision_error(self) -> None:
        envelope = marshal_error(ProvisionError("adapter machinery is base-wheel only"))
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

    def test_protocol_error_maps_to_protocol_error(self) -> None:
        envelope = marshal_error(ProtocolError("bad envelope"))
        assert envelope["error_type"] == "ProtocolError"
        assert envelope["fatal"] is True


class TestMarshalResult:
    def test_result_envelope_wraps_payload_verbatim(self) -> None:
        payload = {"trial_id": "task-1:0", "trajectory": {"messages": []}}
        envelope = marshal_result(payload)
        assert envelope == {"v": 1, "type": "result", "result": payload}


class TestDriveRunTrial:
    """The end-to-end ``_drive_run_trial(stdin, stdout)`` path — envelope in,
    JSON-Lines wire out, exit code returned. Exercised without a real
    runner service; the subset's ``_run_from_start`` fails loudly with a
    :class:`ProvisionError` before it reaches the gRPC connection check
    for envelopes missing the required fields, so we can lock the wire
    contract without spinning anything up."""

    def test_garbage_stdin_emits_protocol_error_envelope(self) -> None:
        import io

        from tolokaforge.runner._cli import _drive_run_trial

        stdin = io.StringIO("not a valid start envelope\n")
        stdout = io.StringIO()
        exit_code = _drive_run_trial(stdin, stdout)
        assert exit_code == 1
        # Exactly one wire line, well-formed JSON, v:1 error envelope.
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected exactly one wire line, got {len(lines)}"
        import json as _json

        envelope = _json.loads(lines[0])
        assert envelope["v"] == 1
        assert envelope["type"] == "error"
        assert envelope["error_type"] == "ProtocolError"
        assert envelope["fatal"] is True

    def test_empty_stdin_emits_cancelled_envelope(self) -> None:
        import io

        from tolokaforge.runner._cli import _drive_run_trial

        stdin = io.StringIO("")
        stdout = io.StringIO()
        exit_code = _drive_run_trial(stdin, stdout)
        assert exit_code == 1
        import json as _json

        envelope = _json.loads(stdout.getvalue().splitlines()[0])
        assert envelope["error_type"] == "cancelled"

    def test_cancel_envelope_before_start_emits_cancelled(self) -> None:
        import io

        from tolokaforge.runner._cli import _drive_run_trial

        stdin = io.StringIO('{"v":1,"type":"cancel"}\n')
        stdout = io.StringIO()
        exit_code = _drive_run_trial(stdin, stdout)
        assert exit_code == 1
        import json as _json

        envelope = _json.loads(stdout.getvalue().splitlines()[0])
        assert envelope["error_type"] == "cancelled"

    def test_start_without_task_emits_provision_error(self) -> None:
        """The subset-native shim needs the caller to supply enough that a
        TrialSpec could be materialised; a bare envelope with an empty
        ``task`` surfaces as ProvisionError before any gRPC connection
        attempt, so the wire never lies about what happened."""
        import io

        from tolokaforge.runner._cli import _drive_run_trial

        stdin = io.StringIO('{"v":1,"type":"start"}\n')
        stdout = io.StringIO()
        exit_code = _drive_run_trial(stdin, stdout)
        assert exit_code == 1
        import json as _json

        envelope = _json.loads(stdout.getvalue().splitlines()[0])
        assert envelope["error_type"] == "ProvisionError"
        assert "task" in envelope["message"].lower()


class TestVersionResolution:
    """``tolokaforge --version`` resolves via ``importlib.metadata`` against
    the subset distribution name — inside the runner image the lookup
    succeeds; outside (dev-checkout without the subset wheel installed)
    the lookup fails loudly rather than misreporting."""

    def test_subset_distribution_name_matches_hatch_builder(self) -> None:
        """Single source of truth check: the shim's ``SUBSET_DISTRIBUTION_NAME``
        constant must match the name the custom hatch builder writes into
        METADATA. A silent drift would mean the shim asks importlib for a
        name pip never registered inside the image.

        The builder script transitively imports ``hatchling.builders.wheel``,
        which is a build-time-only dep — importing the module directly from
        pytest would need hatchling installed in the dev env. Instead we
        parse the ``SUBSET_DISTRIBUTION_NAME`` assignment out of the source
        with ``ast``, which keeps the check hermetic and hatchling-free.
        """
        import ast
        from pathlib import Path

        from tolokaforge.runner._cli import SUBSET_DISTRIBUTION_NAME as SHIM_NAME

        repo_root = Path(__file__).resolve().parents[2]
        builder_path = repo_root / "scripts" / "hatch" / "hatch_runner_subset_builder.py"
        tree = ast.parse(builder_path.read_text(encoding="utf-8"))
        hatch_name: str | None = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "SUBSET_DISTRIBUTION_NAME":
                        assert isinstance(node.value, ast.Constant)
                        hatch_name = node.value.value
        assert (
            hatch_name is not None
        ), "SUBSET_DISTRIBUTION_NAME assignment not found in hatch_runner_subset_builder.py"
        assert hatch_name == SHIM_NAME
