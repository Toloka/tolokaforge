"""The ``ToolWrapper`` base contract: text plus the substrate's failure verdict.

``execute`` answers with the tool's output text; ``execute_call`` answers with
that text *and* whether the substrate declared the call a failure. Only a
substrate with an out-of-band failure channel (MCP's ``isError``) overrides
``execute_call`` — every other wrapper signals a failed call by raising, and the
golden-replay loop records that raise. Both halves of that invariant are locked
here, because both are load-bearing and neither is visible from the type
signature alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.rag_client import SearchResponse
from tolokaforge.runner.service import _backstop_seconds
from tolokaforge.runner.tool_factory import (
    BuiltinGenericToolWrapper,
    DockerComposeExecToolWrapper,
    PersistentShellToolWrapper,
    RAGSearchToolWrapper,
    ToolCallOutcome,
    ToolWrapper,
    create_search_kb_schema,
)
from tolokaforge.tools.builtin import build_check as build_check_module
from tolokaforge.tools.persistent_shell import CommandResult

pytestmark = pytest.mark.unit

TRIAL_DEFAULT_S = 30.0
"""The trial-level fallback, distinct from every budget below so a resolution
that reached for it instead of the tool would be visible."""

DECLARED_SCHEMA_BUDGET_S = 7.0
"""What a native pack's adapter pins on the tool's ``ToolSchema``."""

OWN_BUDGET_S = 45.0
"""What a self-bounding wrapper applies to its own call — above both, so a band
resolved from either of them instead would be visible."""


def _schema(name: str) -> ToolSchema:
    return ToolSchema(name=name, description=f"{name} for the contract test", parameters={})


class _AnsweringWrapper(ToolWrapper):
    """A wrapper over a substrate with no failure channel: it answers or raises."""

    def __init__(self, tool_schema: ToolSchema, answer: str | Exception):
        super().__init__(tool_schema)
        self._answer = answer

    async def execute(self, arguments: dict[str, Any]) -> str:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


async def test_inherited_execute_call_reports_the_text_as_a_non_failure() -> None:
    wrapper = _AnsweringWrapper(_schema("get_customer"), '{"id": "C-101"}')

    outcome = await wrapper.execute_call({"customer_id": "C-101"})

    assert outcome == ToolCallOutcome(output='{"id": "C-101"}', declared_failure=False)


async def test_inherited_execute_call_lets_a_raising_tool_raise() -> None:
    """A raise must travel out of ``execute_call`` untouched.

    Raising is how every wrapper without an out-of-band failure channel reports a
    failed call, and the golden-replay loop's ``except Exception`` is what records
    it. A ``try/except`` inside the default ``execute_call`` would convert every
    raised tau / MCP-async golden failure into ``declared_failure=False`` and
    record nothing, so this assertion — not the docker-gated replay suite — is
    what makes that regression visible on a pull request.
    """
    boom = RuntimeError("db-service refused the connection")
    wrapper = _AnsweringWrapper(_schema("place_order"), boom)

    with pytest.raises(RuntimeError, match="db-service refused the connection") as excinfo:
        await wrapper.execute_call({"customer_id": "C-101"})

    assert excinfo.value is boom


def test_a_wrapper_enforcing_nothing_is_backstopped_at_its_declared_budget() -> None:
    """Nothing to sit above: the band the runner applies is the declaration itself."""
    schema = _schema("get_customer")
    schema.timeout_s = 12.5
    wrapper = _AnsweringWrapper(schema, "{}")

    assert wrapper.effective_timeout_s == 12.5
    assert _backstop_seconds(wrapper, TRIAL_DEFAULT_S) == 12.5


def _record_persistent_shell_budget(
    wrapper: PersistentShellToolWrapper, monkeypatch: pytest.MonkeyPatch
) -> list[float]:
    """Stand a recorder in for the session ``execute`` hands its budget to."""
    recorded: list[float] = []

    class _RecordingSession:
        def run(self, command: str, timeout_s: float) -> CommandResult:
            recorded.append(timeout_s)
            return CommandResult(output="", exit_code=0, timed_out=False)

    wrapper._session = _RecordingSession()
    return recorded


def _record_compose_exec_budget(
    wrapper: DockerComposeExecToolWrapper, monkeypatch: pytest.MonkeyPatch
) -> list[float]:
    """Stand a recorder in for the ``docker exec`` ``execute`` hands its budget to."""
    recorded: list[float] = []

    def _exec_sync(command: str, timeout: float) -> str:
        recorded.append(timeout)
        return ""

    wrapper._exec_sync = _exec_sync
    return recorded


def _record_build_check_budget(
    wrapper: BuiltinGenericToolWrapper, monkeypatch: pytest.MonkeyPatch
) -> list[float]:
    """Stand a recorder in for the ``httpx`` call the wrapped builtin bounds itself with."""
    recorded: list[float] = []

    class _Response:
        status_code = 200
        text = "ok"

    def _post(url, *, headers=None, content=None, timeout=None):
        recorded.append(timeout)
        return _Response()

    monkeypatch.setattr(build_check_module.httpx, "post", _post)
    return recorded


def _record_search_kb_budget(
    wrapper: RAGSearchToolWrapper, monkeypatch: pytest.MonkeyPatch
) -> list[float]:
    """Stand a recorder in for the RAG request ``execute`` hands its budget to."""
    recorded: list[float] = []

    class _RecordingRagClient:
        async def search(self, trial_id, query, limit, alpha, timeout):
            recorded.append(timeout)
            return SearchResponse(results=[], query=query, trial_id=trial_id, total_results=0)

    wrapper.rag_client = _RecordingRagClient()
    return recorded


def _persistent_shell(own_budget_s: float) -> PersistentShellToolWrapper:
    schema = _schema("bash_session")
    schema.timeout_s = DECLARED_SCHEMA_BUDGET_S
    schema.tool_config = {"timeout_s": own_budget_s}
    return PersistentShellToolWrapper(schema)


def _compose_exec(own_budget_s: float) -> DockerComposeExecToolWrapper:
    schema = _schema("run_command")
    schema.timeout_s = own_budget_s
    return DockerComposeExecToolWrapper(schema, service="app", compose_project_prefix="p")


def _build_check(own_budget_s: float) -> BuiltinGenericToolWrapper:
    """The inversion-visible case: a builtin bounding itself far above its schema.

    Every native pack pins ``ToolSchema.timeout_s`` to one value (#1147), so
    banding on the schema alone would cut this call short at a fraction of the
    budget the tool declares and record a healthy build as a timeout.
    """
    schema = _schema("build_check")
    schema.timeout_s = DECLARED_SCHEMA_BUDGET_S
    schema.tool_config = {"service": "app", "timeout_s": own_budget_s}
    return BuiltinGenericToolWrapper(schema)


def _search_kb(own_budget_s: float) -> RAGSearchToolWrapper:
    schema = create_search_kb_schema()
    schema.timeout_s = own_budget_s
    return RAGSearchToolWrapper(tool_schema=schema, rag_client=None, trial_id="t:0")


@pytest.mark.parametrize(
    ("build", "record", "arguments"),
    [
        (_persistent_shell, _record_persistent_shell_budget, {"command": "true"}),
        (_compose_exec, _record_compose_exec_budget, {"command": "true"}),
        (_build_check, _record_build_check_budget, {}),
        (_search_kb, _record_search_kb_budget, {"query": "anything"}),
    ],
    ids=["bash_session", "docker_compose_exec", "build_check", "search_kb"],
)
async def test_a_wrapper_enforcing_its_own_budget_is_backstopped_strictly_above_it(
    build, record, arguments, monkeypatch
) -> None:
    """The two budgets are read from the two paths that use them, never asserted
    to be some literal: the one ``execute`` hands its own timeout mechanism, and
    the one the runner resolves for its backstop. The backstop must sit strictly
    above, because the tool's timeout terminates the work and keeps the session
    usable while the backstop only abandons the worker thread.
    """
    wrapper = build(OWN_BUDGET_S)
    recorded = record(wrapper, monkeypatch)

    await wrapper.execute(arguments)

    assert recorded == [OWN_BUDGET_S], (
        "the wrapper did not hand its own per-call budget to the mechanism that "
        f"enforces it: {recorded}"
    )
    backstop = _backstop_seconds(wrapper, TRIAL_DEFAULT_S)
    assert backstop > recorded[0], (
        f"the runner's {backstop}s backstop does not clear the {recorded[0]}s budget "
        "the tool enforces itself — the two controls race"
    )
