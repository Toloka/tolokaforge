"""Keyless shape lock for the ``mock_web_booking`` example pack.

The pack (``examples/native/mock_web_booking/``) drives the first-party
mock-web service over ``http_request`` and grades on the confirmation number
mock-web issues. This guard runs on every PR without Docker or a provider key:
it loads the pack through the same loaders the CLI uses and asserts the load-
bearing shape — the ``http_request`` allow-list, the mock-web endpoint the task
targets, and the two transcript gates that make grading meaningful. It is a
*shape* lock; the paid behaviour lock (a graded ``TrialResult`` through the
composed stack) lives in the deploy integration lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.models import GradingConfig

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "mock_web_booking"
_TASK_YAML = _PACK_ROOT / "dataset" / "tasks" / "booking_01" / "task.yaml"
_GRADING_YAML = _PACK_ROOT / "dataset" / "tasks" / "booking_01" / "grading.yaml"

_MOCK_WEB_HOST = "mock-web:8080"
_BOOKING_URL = "http://mock-web:8080/booking"
_CONFIRMATION_TOKEN = "BKSEA12345"


def test_task_loads_and_enables_http_request_to_mock_web() -> None:
    """The task validates and grants only ``http_request`` allow-listed to mock-web.

    Locks the tool contract: the pack drives mock-web through ``http_request``
    (not a browser or a bespoke tool), and the allow-list names the peer — so
    dropping the tool or repointing the host trips CI keylessly.
    """
    assert _TASK_YAML.is_file(), f"pack task.yaml is missing: {_TASK_YAML}"
    task, _ = load_task_yaml(_TASK_YAML)

    agent_tools = task.tools.agent
    enabled = agent_tools.get("enabled")
    assert enabled == ["http_request"], f"agent must enable only http_request, got: {enabled!r}"
    hosts = agent_tools.get("http_request", {}).get("allowed_hosts", [])
    assert _MOCK_WEB_HOST in hosts, f"allow-list must include {_MOCK_WEB_HOST!r}, got: {hosts!r}"


def test_task_targets_the_mock_web_booking_endpoint() -> None:
    """The task text points the agent at the mock-web booking site.

    Combines the initial user message and the policy guidance and requires the
    ``/booking`` URL — so repointing the pack away from mock-web trips here.
    """
    task, _ = load_task_yaml(_TASK_YAML)
    guidance = task.policies.get("guidance", [])
    task_text = "\n".join([task.initial_user_message or "", *guidance])
    assert _BOOKING_URL in task_text, f"task text must target {_BOOKING_URL!r}"


def _load_grading() -> tuple[dict, GradingConfig]:
    raw = yaml.safe_load(_GRADING_YAML.read_text())
    return raw, GradingConfig(**raw)


def test_grading_is_transcript_only_with_both_gates() -> None:
    """Grading validates and locks the two product-scored transcript gates.

    Asserts the confirmation-number token, the POST ``required_actions`` gate,
    and that ``combine`` weights only ``transcript_rules`` — so dropping either
    gate, repointing the graded value, or adding a keyed family (llm_judge /
    state_checks) that a keyless run cannot satisfy trips CI without Docker.
    """
    assert _GRADING_YAML.is_file(), f"pack grading.yaml is missing: {_GRADING_YAML}"
    raw, grading = _load_grading()

    assert set(grading.combine.weights) == {"transcript_rules"}, (
        "grading must weight only transcript_rules (no llm_judge / state_checks a "
        f"keyless run cannot satisfy), got weights: {grading.combine.weights!r}"
    )
    assert grading.state_checks is None, "grading must not use state_checks"
    assert grading.llm_judge is None, "grading must not use an llm_judge"

    rules = grading.transcript_rules
    assert rules is not None, "grading must define transcript_rules"
    assert _CONFIRMATION_TOKEN in rules.must_contain, (
        f"grading must require the mock-web confirmation token {_CONFIRMATION_TOKEN!r} in "
        f"must_contain, got: {rules.must_contain!r}"
    )

    post_gates = [
        action
        for action in rules.required_actions
        if action.name == "http_request" and action.arguments.get("method") == "POST"
    ]
    assert post_gates, (
        "grading must gate on an http_request POST required_action (proves the booking "
        f"round-trip happened), got: {rules.required_actions!r}"
    )
    assert "method" in (post_gates[0].compare_args or []), (
        "the POST gate must compare the method argument, else it is a silent no-op; "
        f"got compare_args: {post_gates[0].compare_args!r}"
    )
