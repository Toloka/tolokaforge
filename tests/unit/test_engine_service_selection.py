"""The run starts each optional engine service its tasks need, and only those (#125, #1937).

``core_stack`` only starts ``db-service`` + ``runner``. Tasks that resolve
``http://mock-web:8080/...`` (browser, mobile, an ``http_request`` whose hosts
admit mock-web, or ``initial_state.mock_web``) need mock-web; tasks that search
with a backend declaring the rag-service stack service (the default
``rag_service``) need rag-service. The two are resolved one by one, so a run
that needs one does not start the other. Detection mirrors
``_tasks_need_playwright``: scan the actors' tool blocks, look for
``initial_state.mock_web``, and ask the task's declared search backend (for a
task that declares a corpus or enables its search tool) which stack service it
needs (ADR-0054). A backend that runs in the runner alone needs neither.

Adapters whose search signal is not visible in task tool names (e.g. a
domain-shipped ``docindex/`` knowledge base surfaced as
``TaskDescription.search.enabled``) declare the rag-service need via
``DockerStackRequirements.needs_rag_service``. The run config's
``docker.services`` then starts a service ``always`` or forbids it (``never``),
and forbidding one the run needs is refused (ADR-0065).
"""

from __future__ import annotations

import pytest

from tests.utils.search_backends import register_search_backends
from tolokaforge.adapters.base import DockerStackRequirements
from tolokaforge.core.engine_stack_runtime import (
    EngineServiceConflictError,
    resolve_engine_services,
)
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.models.docker_config import EngineServicesConfig
from tolokaforge.testing.search_backends import in_memory_search_backend_factory

pytestmark = pytest.mark.unit

CORE = (False, False)
RAG = (True, False)
WEB = (False, True)
BOTH = (True, True)


def _task(
    enabled_tools: list[str] | None = None,
    initial_state: dict | None = None,
    user_tools: list[str] | None = None,
    task_id: str = "t",
) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name="t",
        category="x",
        description="x",
        max_turns=1,
        initial_user_message="x",
        initial_state=initial_state or {},
        tools={
            "agent": {"enabled": enabled_tools or []},
            "user": {"enabled": user_tools or []},
        },
        actors={"user": {"mode": "llm"}},
        grading="grading.yaml",
    )


def _task_with_tool_blocks(agent: dict, user: dict | None = None) -> TaskConfig:
    return TaskConfig(
        task_id="t",
        name="t",
        category="x",
        description="x",
        max_turns=1,
        initial_user_message="x",
        tools={"agent": agent, "user": user or {"enabled": []}},
        actors={"user": {"mode": "llm"}},
        grading="grading.yaml",
    )


def _starts(
    tasks: list[TaskConfig],
    requirements: DockerStackRequirements | None = None,
    **services: str,
) -> tuple[bool, bool]:
    """Whether the run starts (rag-service, mock-web)."""
    plan = resolve_engine_services(tasks, requirements, EngineServicesConfig(**services))
    return plan.rag_service.start, plan.mock_web.start


def test_a_browser_tool_starts_mock_web_alone():
    assert _starts([_task(["browser"])]) == WEB


def test_a_mobile_tool_starts_mock_web_alone():
    assert _starts([_task(["mobile"])]) == WEB


def test_a_search_kb_tool_starts_rag_service_alone():
    assert _starts([_task(["search_kb"])]) == RAG


def test_initial_state_mock_web_starts_mock_web_alone():
    assert _starts([_task(initial_state={"mock_web": {"base_url": "http://mock-web:8080"}})]) == WEB


def test_an_initial_state_rag_corpus_starts_rag_service_alone():
    assert _starts([_task(initial_state={"rag": {"corpus_dir": "rag/corpus"}})]) == RAG


def test_an_empty_initial_state_rag_starts_nothing():
    """``rag: {}`` declares no corpus: a typed block is truthy, the corpus is what counts."""
    assert _starts([_task(initial_state={"rag": {}})]) == CORE


def test_a_renamed_search_tool_on_the_default_backend_starts_rag_service():
    task = _task(["lookup_docs"], initial_state={"rag": {"tool": {"name": "lookup_docs"}}})
    assert _starts([task]) == RAG


def test_a_rag_block_that_searches_nothing_starts_nothing():
    """No corpus and no enabled search tool: the backend would serve nothing."""
    task = _task(["bash"], initial_state={"rag": {"backend": "rag_service"}})
    assert _starts([task]) == CORE


class TestTheBuiltInBm25BackendKeepsTheCoreStack:
    """``bm25`` runs in the runner process and declares no stack service."""

    def test_its_corpus_and_search_kb_tool_keep_the_core_stack(self):
        rag = {"corpus_dir": "kb", "backend": "bm25"}
        assert _starts([_task(["search_kb"], initial_state={"rag": rag})]) == CORE

    def test_a_configured_bm25_task_keeps_the_core_stack(self):
        rag = {
            "corpus_dir": "kb",
            "backend": "bm25",
            "backend_config": {"render": {"kind": "text"}, "ranking": {"top_k": 3}},
            "tool": {"name": "search_docs"},
        }
        assert _starts([_task(["search_docs"], initial_state={"rag": rag})]) == CORE

    def test_a_rag_service_task_beside_it_still_starts_rag_service(self):
        bm25 = _task(["search_kb"], initial_state={"rag": {"backend": "bm25", "corpus_dir": "kb"}})
        assert _starts([bm25, _task(["search_kb"])]) == RAG


class TestABackendWithoutAStackService:
    """The backend's declaration selects the service; a tool named ``search_kb`` does not."""

    @pytest.fixture(autouse=True)
    def _in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        register_search_backends(monkeypatch, in_memory=in_memory_search_backend_factory)

    def test_its_search_kb_tool_keeps_the_core_stack(self):
        task = _task(["search_kb"], initial_state={"rag": {"backend": "in_memory"}})
        assert _starts([task]) == CORE

    def test_its_corpus_keeps_the_core_stack(self):
        rag = {"corpus_dir": "kb", "backend": "in_memory", "tool": {"name": "lookup_docs"}}
        task = _task(["lookup_docs"], initial_state={"rag": rag})
        assert _starts([task]) == CORE

    def test_a_default_backend_task_beside_it_still_starts_rag_service(self):
        in_memory = _task(["search_kb"], initial_state={"rag": {"backend": "in_memory"}})
        assert _starts([in_memory, _task(["search_kb"])]) == RAG


def test_one_search_task_among_others_starts_rag_service():
    assert _starts([_task(["bash"]), _task(["search_kb", "read_file"])]) == RAG


def test_a_browser_task_and_a_search_task_start_both():
    assert _starts([_task(["browser"]), _task(["search_kb"])]) == BOTH


def test_tools_that_reach_neither_service_keep_the_core_stack():
    assert _starts([_task(["bash", "calculator", "read_file"])]) == CORE


def test_a_user_declared_browser_starts_mock_web():
    """The user simulator's tools run in the same container the agent's do.

    A ``browser`` the user declares reaches mock-web exactly as the agent's does,
    so reading ``tools.agent`` alone would start a core stack with no mock-web and
    the tool would fail at its first call.
    """
    assert _starts([_task(user_tools=["browser"])]) == WEB
    assert _starts([_task(user_tools=[])]) == CORE


class TestAnHttpRequestThatReachesMockWeb:
    """``http_request`` requests mock-web on its default hosts (#1935).

    A task that enables it with no ``allowed_hosts``, or with mock-web among them,
    calls ``http://mock-web:8080``; with no mock-web that request has no service to
    reach. The selection reads the hosts the task declares: a task that names other
    hosts only, an app world's for one, does not ask for mock-web.
    """

    def test_its_default_hosts_start_mock_web(self):
        assert _starts([_task(["http_request"])]) == WEB

    def test_an_empty_host_list_falls_back_to_the_default_hosts(self):
        task = _task_with_tool_blocks(
            {"enabled": ["http_request"], "http_request": {"allowed_hosts": []}}
        )
        assert _starts([task]) == WEB

    @pytest.mark.parametrize("host", ["mock-web", "mock-web:8080"])
    def test_allowed_hosts_naming_mock_web_start_mock_web(self, host):
        task = _task_with_tool_blocks(
            {"enabled": ["http_request"], "http_request": {"allowed_hosts": [host]}}
        )
        assert _starts([task]) == WEB

    def test_allowed_hosts_without_mock_web_keep_the_core_stack(self):
        task = _task_with_tool_blocks(
            {
                "enabled": ["http_request"],
                "http_request": {"allowed_hosts": ["helpdesk.vendor.example"]},
            }
        )
        assert _starts([task]) == CORE

    def test_mock_web_on_another_port_keeps_the_core_stack(self):
        task = _task_with_tool_blocks(
            {"enabled": ["http_request"], "http_request": {"allowed_hosts": ["mock-web:9090"]}}
        )
        assert _starts([task]) == CORE

    def test_localhost_alone_keeps_the_core_stack(self):
        """The runner container's ``localhost`` is the runner, not mock-web."""
        task = _task_with_tool_blocks(
            {"enabled": ["http_request"], "http_request": {"allowed_hosts": ["localhost:8080"]}}
        )
        assert _starts([task]) == CORE

    def test_a_user_declared_http_request_starts_mock_web(self):
        assert _starts([_task(user_tools=["http_request"])]) == WEB

    def test_a_host_list_without_the_tool_enabled_keeps_the_core_stack(self):
        task = _task_with_tool_blocks(
            {"enabled": ["bash"], "http_request": {"allowed_hosts": ["mock-web:8080"]}}
        )
        assert _starts([task]) == CORE


def test_an_empty_task_list_keeps_the_core_stack():
    assert _starts([]) == CORE


class TestTheAdaptersDeclaredNeed:
    def test_it_starts_rag_service_alone(self):
        """The task-level signals see nothing (plain tools), but the adapter
        declares search-enabled TaskDescriptions - the run must get rag-service,
        and nothing that only a browser would need."""
        reqs = DockerStackRequirements(needs_rag_service=True)
        assert _starts([_task(["bash"])], reqs) == RAG

    def test_default_requirements_start_nothing(self):
        assert _starts([_task(["bash"])], DockerStackRequirements()) == CORE

    def test_no_requirements_fall_back_to_task_signals(self):
        assert _starts([_task(["bash"])], None) == CORE
        assert _starts([_task(["search_kb"])], None) == RAG

    def test_task_signals_still_count_with_default_requirements(self):
        assert _starts([_task(["search_kb"])], DockerStackRequirements()) == RAG

    def test_needs_rag_service_is_not_rendered_into_stack_kwargs(self):
        """``needs_rag_service`` feeds the service selection; it must NOT leak into
        ``core_stack(**kwargs)`` / ``full_stack(**kwargs)`` - neither factory
        accepts it, so a leak would TypeError at stack construction."""
        reqs = DockerStackRequirements(needs_rag_service=True)
        assert reqs.to_core_stack_kwargs() == {}


class TestTheRunConfigOverride:
    """``docker.services.<service>``: ``auto`` (default), ``always`` or ``never``."""

    @pytest.mark.parametrize(
        ("services", "expected"),
        [({"rag_service": "always"}, RAG), ({"mock_web": "always"}, WEB)],
    )
    def test_always_starts_a_service_no_task_needs(self, services, expected):
        assert _starts([_task(["bash"])], **services) == expected

    def test_never_on_a_service_no_task_needs_starts_nothing(self):
        assert _starts([_task(["bash"])], rag_service="never", mock_web="never") == CORE

    def test_never_leaves_the_other_service_to_its_signals(self):
        assert _starts([_task(["browser"])], rag_service="never") == WEB

    def test_never_on_a_service_a_task_needs_is_refused_naming_the_task(self):
        with pytest.raises(EngineServiceConflictError) as refusal:
            _starts([_task(["browser"], task_id="booking")], mock_web="never")
        message = str(refusal.value)
        assert "docker.services.mock_web is 'never'" in message
        assert "task booking" in message

    def test_never_on_a_service_the_adapter_needs_is_refused_naming_the_adapter(self):
        reqs = DockerStackRequirements(needs_rag_service=True)
        with pytest.raises(EngineServiceConflictError, match="the adapter"):
            _starts([_task(["bash"])], reqs, rag_service="never")

    def test_the_refusal_names_the_adapter_before_the_tasks(self):
        """However many tasks need the service too, the adapter is never cut from the message."""
        reqs = DockerStackRequirements(needs_rag_service=True)
        tasks = [_task(["search_kb"], task_id=f"t{index}") for index in range(4)]
        with pytest.raises(
            EngineServiceConflictError,
            match=r"the adapter \(needs_rag_service\), task t0, task t1 and 2 more",
        ):
            _starts(tasks, reqs, rag_service="never")


class TestTheReasons:
    """The run's log says why each service starts or not."""

    def _plan(self, tasks, **services):
        return resolve_engine_services(tasks, None, EngineServicesConfig(**services))

    def test_a_needed_service_names_what_needs_it(self):
        plan = self._plan([_task(["browser"], task_id="shop")])
        assert plan.mock_web.reason == "needed by task shop"

    def test_an_unneeded_service_says_so(self):
        assert self._plan([_task(["bash"])]).rag_service.reason == "no task or adapter needs it"

    def test_an_override_names_itself(self):
        plan = self._plan([_task(["bash"])], rag_service="always", mock_web="never")
        assert plan.rag_service.reason == "docker.services.rag_service is 'always'"
        assert plan.mock_web.reason == "docker.services.mock_web is 'never'"

    def test_many_needers_are_counted_past_the_first_three(self):
        tasks = [_task(["search_kb"], task_id=f"t{index}") for index in range(5)]
        assert (
            self._plan(tasks).rag_service.reason == "needed by task t0, task t1, task t2 and 2 more"
        )
