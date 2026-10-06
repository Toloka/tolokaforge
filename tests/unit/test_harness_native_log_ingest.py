"""Folding a harness's own inner counts, recovered from its native logs.

A harness trial is one tool call, so the engine measures no turns or tokens of
its own. Three surfaces meet here. The coding-harness package parses the
harness's agent-session logs into boundary-safe plain counts
(:func:`~tolokaforge_coding_harnesses.native_log.parse_native_logs`, reached
through :meth:`CodingHarnessAdapterMixin.ingest_native_logs`); the
:class:`BaseAdapter` default recovers nothing; and
:meth:`TrialRunner._apply_harness_native_log_ingest` folds the recovered counts
into the trial's :class:`Metrics` as harness-reported, at the lowest precedence
behind the CLI's stdout totals and the request-middleware wire usage.

The fold is driven directly on the runner's telemetry method so the precedence
between the three taps is asserted without a container; the parse is driven on
the mixin so the ``/logs`` layout is asserted without the engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tolokaforge_coding_harnesses.native_log import (
    NATIVE_LOG_USAGE_SOURCE,
    HarnessNativeLogCounts,
    parse_native_logs,
)
from tolokaforge_coding_harnesses.stdout_telemetry import HarnessStdoutTelemetry
from tolokaforge_coding_harnesses.usage_log import MIDDLEWARE_PROXY_USAGE_SOURCE

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import init_trial_logger
from tolokaforge.core.models import GradingConfig, TaskConfig
from tolokaforge.core.runner import TrialRunner
from tolokaforge_coding_harnesses import CodingHarnessAdapterMixin

pytestmark = pytest.mark.unit

_MODEL = "openrouter/anthropic/claude-sonnet-4.6"
_STAGED = {"logs/agent/session.jsonl": b"<bytes>"}
"""A non-empty staged-artifacts mapping — the fold's gate is that native
artifacts were staged, not what they contain (the ingest callable is what reads
them, and the tests substitute it)."""


def _agent_log(*records: dict[str, Any]) -> bytes:
    """One JSONL agent-session log carrying *records*, one object per line."""
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


# --------------------------------------------------------------------------- #
# The adapter hook: default recovers nothing, the mixin parses the /logs tree.
# --------------------------------------------------------------------------- #


class _PlainAdapter(BaseAdapter):
    """A concrete adapter overriding none of the optional hooks.

    Every abstract method raises — none is reached here — so the only behaviour
    under test is the inherited :meth:`ingest_native_logs` default.
    """

    def get_task_ids(self) -> list[str]:  # pragma: no cover - never called
        raise NotImplementedError

    def get_task(self, task_id: str) -> TaskConfig:  # pragma: no cover
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:  # pragma: no cover
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(
        self, task_id: str, env: AdapterEnvironment
    ) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> GradingConfig:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(
        self, task_id: str, env: AdapterEnvironment
    ) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def test_base_adapter_ingests_nothing_by_default() -> None:
    """The default hook recovers nothing even from well-formed agent logs."""
    adapter = _PlainAdapter({})
    logs = {"logs/agent/s.jsonl": _agent_log({"role": "assistant"})}
    assert adapter.ingest_native_logs("any-task", logs) is None


def test_the_mixin_parses_turns_and_token_usage() -> None:
    """A Harbor / terminal-bench adapter inherits the ``/logs`` agent-session parse."""
    logs = {
        "logs/agent/session.jsonl": _agent_log(
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"role": "assistant"},
        ),
        "logs/verifier/reward.txt": b"1.0\n",
    }
    counts = CodingHarnessAdapterMixin().ingest_native_logs("any-task", logs)
    assert counts == HarnessNativeLogCounts(
        turns=2,
        prompt_tokens=10,
        completion_tokens=5,
        reasoning_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def test_the_mixin_folds_anthropic_cache_into_the_inclusive_prompt() -> None:
    """``input_tokens`` is the non-cached remainder, so cache is folded to the
    inclusive total the engine prices on."""
    logs = {
        "logs/agent/s.jsonl": _agent_log(
            {
                "role": "assistant",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                },
            }
        )
    }
    counts = parse_native_logs(logs)
    assert counts is not None
    assert counts.prompt_tokens == 130
    assert counts.cache_read_input_tokens == 100
    assert counts.cache_creation_input_tokens == 20


def test_the_mixin_recovers_turns_when_the_logs_carry_no_usage() -> None:
    """Turns are countable where tokens are not — the token fields stay ``None``
    rather than reporting a spurious zero."""
    logs = {"logs/agent/s.jsonl": _agent_log({"role": "assistant"}, {"role": "assistant"})}
    counts = CodingHarnessAdapterMixin().ingest_native_logs("any-task", logs)
    assert counts is not None
    assert counts.turns == 2
    assert counts.has_token_counts is False


def test_the_mixin_returns_none_when_no_agent_logs_are_present() -> None:
    """Only the verifier's reward survived; there is no session to count."""
    assert (
        CodingHarnessAdapterMixin().ingest_native_logs(
            "any-task", {"logs/verifier/reward.txt": b"1.0\n"}
        )
        is None
    )


def test_the_mixin_returns_none_on_malformed_logs_without_raising() -> None:
    """Non-UTF-8 bytes and non-JSON lines are skipped, not raised on."""
    logs = {"logs/agent/s.jsonl": b"not json\nstill not\n\xff\xfe"}
    assert CodingHarnessAdapterMixin().ingest_native_logs("any-task", logs) is None


# --------------------------------------------------------------------------- #
# The runner fold: precedence stdout > wire > native-log, attribution, no-ops.
# --------------------------------------------------------------------------- #


class _StubAgentClient:
    """The CLI ran the model, so the client issues nothing — only identity is read."""

    model_name = _MODEL


class _InertExecutor:
    """A tool executor the direct fold tests never drive."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the fold step issues no tool call")


def _runner() -> TrialRunner:
    runner = TrialRunner(
        task_id="native-log",
        trial_index=0,
        agent_client=_StubAgentClient(),  # type: ignore[arg-type]
        user_simulator=None,
        tool_executor=_InertExecutor(),
        tool_schemas=[],
        episode_timeout_s=600,
    )
    runner.logger = init_trial_logger("native-log:0", verbose=False, strict=False)
    return runner


def _ingest(counts: HarnessNativeLogCounts | None):
    return lambda task_id, native_files: counts


def _stdout(
    *, turns: int, prompt_tokens: int | None, dialect: str = "claude-code/stream-json"
) -> HarnessStdoutTelemetry:
    return HarnessStdoutTelemetry(
        dialect=dialect,
        turns=turns,
        cost_usd=None,
        duration_s=None,
        prompt_tokens=prompt_tokens,
        completion_tokens=prompt_tokens,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )


def test_the_fold_populates_turns_usage_and_labels_them_harness_reported() -> None:
    runner = _runner()
    runner._harness_native_artifacts = _STAGED
    runner._harness_native_log_ingest = _ingest(
        HarnessNativeLogCounts(turns=7, prompt_tokens=100, completion_tokens=20)
    )

    runner._apply_harness_native_log_ingest()

    assert runner.metrics.turns == 7
    assert runner.metrics.usage.prompt_tokens == 100
    assert runner.metrics.usage.completion_tokens == 20
    # Reads as harness-reported, never engine-measured, in the three-state contract.
    assert runner.metrics.harness_usage_source == NATIVE_LOG_USAGE_SOURCE
    # Priced through the one harness pricing authority, not left unpriced.
    assert runner.metrics.cost_usd is not None


def test_turns_only_recovery_leaves_usage_and_its_source_untouched() -> None:
    runner = _runner()
    runner._harness_native_artifacts = _STAGED
    runner._harness_native_log_ingest = _ingest(HarnessNativeLogCounts(turns=4))

    runner._apply_harness_native_log_ingest()

    assert runner.metrics.turns == 4
    # No tokens were measured, so the token-tap label stays unset and the trial's
    # usage is the empty default — never a $0 for a trial that ran.
    assert runner.metrics.harness_usage_source is None
    assert runner.metrics.usage == Usage()
    assert runner.metrics.cost_usd is None


def test_the_clis_stdout_turns_win_over_the_native_log() -> None:
    runner = _runner()
    runner._harness_native_artifacts = _STAGED
    # The CLI printed turns but no tokens (kimi-style); stdout already set turns.
    runner._harness_stdout_telemetry = _stdout(turns=12, prompt_tokens=None)
    runner.metrics.turns = 12
    runner._harness_native_log_ingest = _ingest(
        HarnessNativeLogCounts(turns=7, prompt_tokens=100, completion_tokens=20)
    )

    runner._apply_harness_native_log_ingest()

    # Turns stay the CLI's count — the native log does not override them.
    assert runner.metrics.turns == 12
    # But the CLI reported no tokens, so the native-log usage still fills in.
    assert runner.metrics.usage.prompt_tokens == 100
    assert runner.metrics.harness_usage_source == NATIVE_LOG_USAGE_SOURCE


def test_the_clis_stdout_tokens_win_over_the_native_log_usage() -> None:
    runner = _runner()
    runner._harness_native_artifacts = _STAGED
    runner._harness_stdout_telemetry = _stdout(turns=5, prompt_tokens=500)
    runner.metrics.turns = 5
    runner.metrics.usage = Usage(prompt_tokens=500, completion_tokens=500)
    runner._harness_native_log_ingest = _ingest(
        HarnessNativeLogCounts(turns=7, prompt_tokens=100, completion_tokens=20)
    )

    runner._apply_harness_native_log_ingest()

    # Stdout supplied both turns and tokens — the native log changes neither.
    assert runner.metrics.turns == 5
    assert runner.metrics.usage.prompt_tokens == 500
    assert runner.metrics.harness_usage_source is None


def test_the_wire_usage_wins_over_the_native_log_usage() -> None:
    runner = _runner()
    runner._harness_native_artifacts = _STAGED
    # The wire tap already measured tokens (a proxied harness); no CLI stdout.
    runner.metrics.harness_usage_source = MIDDLEWARE_PROXY_USAGE_SOURCE
    runner.metrics.usage = Usage(prompt_tokens=800, completion_tokens=40)
    runner._harness_native_log_ingest = _ingest(
        HarnessNativeLogCounts(turns=7, prompt_tokens=100, completion_tokens=20)
    )

    runner._apply_harness_native_log_ingest()

    # Wire tokens win and keep their own source label.
    assert runner.metrics.usage.prompt_tokens == 800
    assert runner.metrics.harness_usage_source == MIDDLEWARE_PROXY_USAGE_SOURCE
    # The wire tap never set turns, so the native-log turns still apply.
    assert runner.metrics.turns == 7


def test_the_fold_is_a_noop_when_no_native_artifacts_were_staged() -> None:
    """An engine-loop trial, or a run that preserved no native output, stages
    nothing — the ingest never runs even if a callable was handed in."""
    runner = _runner()
    runner._harness_native_log_ingest = _ingest(HarnessNativeLogCounts(turns=9))
    # ``_harness_native_artifacts`` stays ``None``.

    runner._apply_harness_native_log_ingest()

    assert runner.metrics.turns == 0
    assert runner.metrics.harness_usage_source is None


def test_the_fold_is_a_noop_when_no_ingest_callable_was_provided() -> None:
    runner = _runner()
    runner._harness_native_artifacts = _STAGED
    # No ``_harness_native_log_ingest`` set (the common, non-harness case).

    runner._apply_harness_native_log_ingest()

    assert runner.metrics.turns == 0
    assert runner.metrics.harness_usage_source is None


def test_a_raising_ingest_costs_the_trial_nothing() -> None:
    """A defective adapter parse may not fail a trial — the fold swallows it."""
    runner = _runner()
    runner._harness_native_artifacts = _STAGED

    def _boom(task_id: str, native_files: dict[str, bytes]) -> HarnessNativeLogCounts:
        raise RuntimeError("corrupt agent log")

    runner._harness_native_log_ingest = _boom

    runner._apply_harness_native_log_ingest()  # does not raise

    assert runner.metrics.turns == 0
    assert runner.metrics.harness_usage_source is None
