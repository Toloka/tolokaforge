"""A delegated harness's inner counts, recovered from its native logs, reach the report.

A harness trial is one tool call, so the engine measures no turns or tokens.
When the run preserves native artifacts, the harness's own agent-session logs
are staged off the trial container; the resolved adapter's
:meth:`~tolokaforge.adapters.base.BaseAdapter.ingest_native_logs` recovers the
turns and tokens from them, and the runner folds them into the trial's
:class:`Metrics` labelled as harness-reported. This drives a real
``TrialRunner.run_harness`` over a scripted ``docker exec`` tool (no container),
with the shipped :class:`CodingHarnessAdapterMixin` parse, and asserts the
recovered counts both land on the trajectory and flow through the production
run-report aggregation **with no aggregation change**:

- ``avg_turns`` includes the recovered turn count (it has no harness filter),
- the trial reads as harness-reported (``harness_usage_source``), so it is
  excluded from the engine-only ``stuck_rate`` — a single harness trial that
  made one tool call reports no spurious "measured, none stuck" zero.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from typing import Any

import pytest
from tolokaforge_coding_harnesses.native_log import NATIVE_LOG_USAGE_SOURCE

from tolokaforge.core.logging import init_trial_logger
from tolokaforge.core.metrics import calculate_task_metrics, partition_trial_outcomes
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult
from tolokaforge_coding_harnesses import CodingHarnessAdapterMixin

pytestmark = pytest.mark.canonical

# A harness whose CLI declares no stdout dialect and routes through no proxy, so
# the native logs are the only tap that can recover its inner counts.
_HARNESS_WITH_NO_OTHER_TAP = "grok-build"
_MODEL = "openrouter/anthropic/claude-sonnet-4.6"


def _tar_base64(members: dict[str, bytes]) -> str:
    """A base64-encoded tar carrying *members* — what ``tar ... | base64`` prints."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _agent_log(*records: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


# The harness ran three assistant turns inside the container; one carried the
# usage block the CLI never printed on stdout.
_LOGS_SUBTREE = {
    "logs/agent/session.jsonl": _agent_log(
        {"role": "assistant", "usage": {"input_tokens": 40, "output_tokens": 12}},
        {"role": "assistant"},
        {"role": "assistant"},
    ),
    "logs/verifier/reward.txt": b"1.0\n",
}


class _ScriptedHarnessExecutor:
    """A ``docker exec`` bash tool: the CLI's own call, then the artifact read.

    The CLI invocation (any non-``tar`` command) returns its stdout; the engine's
    ``tar ... | base64`` read of ``/logs`` returns the staged subtree. This is the
    two-execution shape a harness trial makes when a run preserves native output.
    """

    def execute(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolResult:
        command = arguments["command"]
        if command.startswith("tar "):
            return ToolResult(success=True, output=_tar_base64(_LOGS_SUBTREE))
        return ToolResult(success=True, output="the harness CLI did its work")


class _StubAgentClient:
    """The CLI ran the model, so the client issues nothing — only identity is read."""

    model_name = _MODEL


def _delegated_harness_trajectory():
    """Run one delegated-harness trial whose adapter recovers inner counts."""
    runner = TrialRunner(
        task_id="recover-from-logs",
        trial_index=0,
        agent_client=_StubAgentClient(),  # type: ignore[arg-type]
        user_simulator=None,
        tool_executor=_ScriptedHarnessExecutor(),
        tool_schemas=[],
        episode_timeout_s=600,
    )
    runner.logger = init_trial_logger("recover-from-logs:0", verbose=False, strict=False)
    # The shipped mixin is the ingest the conductor hands down for Harbor /
    # terminal-bench; ``/logs`` is the artifact root it names.
    mixin = CodingHarnessAdapterMixin()
    return runner.run_harness(
        tool_name="bash",
        command="grok --run",
        instruction="Fix the failing build.",
        timeout_s=600,
        harness=_HARNESS_WITH_NO_OTHER_TAP,
        native_artifact_container_paths=["/logs"],
        ingest_native_logs=mixin.ingest_native_logs,
    )


def test_recovered_counts_land_on_the_trial_labelled_harness_reported() -> None:
    metrics = _delegated_harness_trajectory().metrics

    assert metrics.turns == 3
    assert metrics.usage.prompt_tokens == 40
    assert metrics.usage.completion_tokens == 12
    # The CLI printed nothing on stdout, so the dialect stays null and the source
    # names the native-log tap — the trial reads as harness-reported.
    assert metrics.harness_stdout_dialect is None
    assert metrics.harness_usage_source == NATIVE_LOG_USAGE_SOURCE
    # Priced through the one harness pricing authority rather than left unpriced.
    assert metrics.cost_usd is not None


def test_the_recovered_counts_flow_into_the_run_report_with_no_aggregation_change() -> None:
    trajectory = _delegated_harness_trajectory()

    # The trial is a normal measured trial — folding native logs changes neither
    # its status nor the partition.
    assert partition_trial_outcomes([trajectory]).measured_trials == 1

    report = calculate_task_metrics([trajectory])

    # The recovered turn count reaches the unfiltered turn average.
    assert report["avg_turns"] == 3.0
    # The harness-reported label excludes the one-tool-call trial from the
    # engine-only stuck-rate, so it reports None rather than a spurious 0.0.
    assert report["stuck_rate"] is None
