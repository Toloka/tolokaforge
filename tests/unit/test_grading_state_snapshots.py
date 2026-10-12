"""Host-grader evidence survives serialization without bloating the verdict."""

import pytest
import yaml

from tolokaforge.core.models.grade import Grade, GradingStateSnapshots
from tolokaforge.core.output.artifacts import bundle_redaction
from tolokaforge.core.output_writer import GRADING_STATE_SNAPSHOTS_FILENAME, OutputWriter
from tolokaforge.core.redaction import REDACTED_PLACEHOLDER, NoRedaction, SensitiveKeyRedaction

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("passed", [True, False])
@pytest.mark.parametrize("redact", [True, False])
def test_host_states_written_on_pass_and_fail_without_mutating_evidence(tmp_path, passed, redact):
    states = GradingStateSnapshots(
        source="deterministic replay",
        initial={"items": [{"id": "1", "status": "open"}]},
        golden={"items": [{"id": "1", "status": "closed"}]},
        final={"items": [{"id": "1", "api_token": "example-credential", "status": "open"}]},
    )
    grade = Grade(binary_pass=passed, score=float(passed), state_snapshots=states)
    # Queue / in-memory serialization retains the evidence until the disk writer splits it.
    assert Grade.model_validate_json(grade.model_dump_json()).state_snapshots == states
    writer = OutputWriter(tmp_path, SensitiveKeyRedaction() if redact else NoRedaction())
    writer.write_grade(grade)
    verdict = yaml.safe_load((tmp_path / "grade.yaml").read_text())
    assert verdict["binary_pass"] == passed
    assert "state_snapshots" not in verdict
    stored = GradingStateSnapshots.model_validate(
        yaml.safe_load((tmp_path / GRADING_STATE_SNAPSHOTS_FILENAME).read_text())
    )
    assert stored.initial == states.initial and stored.golden == states.golden
    assert stored.final["items"][0]["api_token"] == (
        REDACTED_PLACEHOLDER if redact else "example-credential"
    )
    assert states.final["items"][0]["api_token"] == "example-credential"
    if redact:
        assert GRADING_STATE_SNAPSHOTS_FILENAME in bundle_redaction(tmp_path).artifacts
        writer.write_grade(Grade(binary_pass=True, score=1))
        assert not (tmp_path / GRADING_STATE_SNAPSHOTS_FILENAME).exists()
        assert GRADING_STATE_SNAPSHOTS_FILENAME not in bundle_redaction(tmp_path).artifacts


def test_legacy_grade_omits_states_and_regrading_removes_old_sidecar(tmp_path):
    grade = Grade(binary_pass=True, score=1)
    assert "state_snapshots" not in grade.model_dump(mode="json")
    old = tmp_path / GRADING_STATE_SNAPSHOTS_FILENAME
    old.write_text("stale evidence")
    OutputWriter(tmp_path).write_grade(grade)
    assert not old.exists()
