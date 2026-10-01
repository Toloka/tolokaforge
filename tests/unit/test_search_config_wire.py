"""``SearchConfig`` on the wire: the backend's name in ``plane``, and two keys that stay off
the wire at their default (ADR-0053).

Every existing task must serialise exactly as before, because the runner and the
grader parse ``TaskDescription`` with ``extra="forbid"`` models: a key an older image
does not declare fails the whole trial at ``RegisterTrial``. The control is the
pre-seam model itself, reproduced here, so "an older image accepts it" is measured
rather than asserted.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from tolokaforge.runner.models import (
    DEFAULT_SEARCH_TOOL_NAME,
    SearchConfig,
    SearchPlane,
    TaskDescription,
)

pytestmark = pytest.mark.unit


class _PreSeamSearchConfig(BaseModel):
    """``SearchConfig`` as an image released before ADR-0053 declares it."""

    enabled: bool = False
    plane: Literal["typesense", "rag_service"] | None = None
    domain_name: str | None = None
    documents_path: str | None = None
    host: str | None = None
    port: int | None = None
    api_key: str | None = None

    model_config = {"extra": "forbid"}


_PRE_SEAM_KEYS = [
    "enabled",
    "plane",
    "domain_name",
    "documents_path",
    "host",
    "port",
    "api_key",
]

_RAG_TASK = {
    "enabled": True,
    "plane": "rag_service",
    "domain_name": "rag_search",
    "documents_path": "rag/corpus",
    "host": None,
    "port": None,
    "api_key": None,
}


@pytest.mark.parametrize(
    "config",
    [
        SearchConfig(),
        SearchConfig(
            enabled=True,
            plane=SearchPlane.RAG_SERVICE,
            domain_name="rag_search",
            documents_path="rag/corpus",
        ),
        SearchConfig(plane=SearchPlane.TYPESENSE, domain_name="retail", documents_path="docindex"),
        SearchConfig(host="typesense", port=8108, api_key="k"),
    ],
    ids=["no-search", "rag-task", "typesense-plane", "typesense-address"],
)
def test_a_task_declaring_no_backend_config_or_tool_name_serialises_as_before(
    config: SearchConfig,
) -> None:
    for dumped in (config.model_dump(mode="json"), json.loads(config.model_dump_json())):
        assert list(dumped) == _PRE_SEAM_KEYS
        assert _PreSeamSearchConfig.model_validate(dumped).model_dump(mode="json") == dumped


def test_the_rag_task_dump_is_byte_identical() -> None:
    config = SearchConfig(
        enabled=True,
        plane=SearchPlane.RAG_SERVICE,
        domain_name="rag_search",
        documents_path="rag/corpus",
    )
    assert config.model_dump_json() == json.dumps(_RAG_TASK, separators=(",", ":"))
    assert config.model_dump() == _RAG_TASK


def test_explicitly_set_defaults_stay_off_the_wire() -> None:
    config = SearchConfig(backend_config={}, tool_name=DEFAULT_SEARCH_TOOL_NAME)
    assert "backend_config" not in config.model_dump(mode="json")
    assert "tool_name" not in config.model_dump(mode="json")


@pytest.mark.parametrize(
    ("declared", "key"),
    [
        ({"backend_config": {"ranking": {"top_k": 3}}}, "backend_config"),
        ({"tool_name": "lookup_docs"}, "tool_name"),
    ],
)
def test_a_declared_value_is_emitted_and_an_older_image_refuses_it(
    declared: dict[str, Any], key: str
) -> None:
    config = SearchConfig(plane="rag_service", **declared)
    dumped = config.model_dump(mode="json")

    assert dumped[key] == declared[key]
    assert SearchConfig.model_validate(dumped) == config
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _PreSeamSearchConfig.model_validate(dumped)


def test_plane_carries_any_backend_name() -> None:
    """The built-in names are constants, not a closed set: a registered name round-trips."""
    config = SearchConfig(plane="a_package_backend")
    assert config.plane == "a_package_backend"
    assert SearchConfig.model_validate(config.model_dump(mode="json")) == config
    with pytest.raises(ValidationError):
        _PreSeamSearchConfig.model_validate(config.model_dump(mode="json"))


def test_the_built_in_constants_serialise_as_their_names() -> None:
    config = SearchConfig(plane=SearchPlane.TYPESENSE)
    assert type(config.plane) is str
    assert config.model_dump()["plane"] == "typesense"


def test_a_mapping_stored_without_validation_dumps_as_the_mapping(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """``model_copy(update=…)`` stores the value unvalidated; the dump must not raise."""
    description = TaskDescription(
        task_id="t",
        name="t",
        category="c",
        description="d",
        adapter_type="native",
        system_prompt="s",
    ).model_copy(update={"search": {"enabled": True, "plane": "rag_service"}})
    assert description.model_dump(mode="json")["search"] == {
        "enabled": True,
        "plane": "rag_service",
    }
    assert json.loads(description.model_dump_json())["search"] == {
        "enabled": True,
        "plane": "rag_service",
    }


def test_the_task_description_carries_the_search_block_unchanged() -> None:
    description = TaskDescription(
        task_id="t",
        name="t",
        category="c",
        description="d",
        adapter_type="native",
        system_prompt="s",
        search=SearchConfig(enabled=True, plane="rag_service", documents_path="kb"),
    )
    assert list(json.loads(description.model_dump_json())["search"]) == _PRE_SEAM_KEYS
    assert list(description.model_dump(mode="json")["search"]) == _PRE_SEAM_KEYS
