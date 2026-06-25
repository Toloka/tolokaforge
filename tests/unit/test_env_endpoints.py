"""Pin the ``EnvEndpoints`` Pydantic model shape + behaviour.

`EnvEndpoints` carries the runner-side service URLs on the wire inside
:class:`tolokaforge.core.trial.TrialSpec`. These tests assert the field
set, the ``extra="forbid"`` strictness, and JSON round-trip identity so
shape drift surfaces at PR time.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.trial import EnvEndpoints

pytestmark = pytest.mark.unit


class TestEnvEndpointsShape:
    def test_required_fields_minimal(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db.local:8000",
            runner_url="http://runner.local:50051",
        )
        assert endpoints.db_url == "http://db.local:8000"
        assert endpoints.runner_url == "http://runner.local:50051"
        # ``rag_url`` is optional — defaults to ``None`` for stacks without RAG.
        assert endpoints.rag_url is None

    def test_rag_url_is_optional(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db:8000",
            rag_url="http://rag:8001",
            runner_url="http://runner:50051",
        )
        assert endpoints.rag_url == "http://rag:8001"

    def test_db_url_is_required(self) -> None:
        with pytest.raises(ValidationError):
            EnvEndpoints(runner_url="http://runner:50051")

    def test_runner_url_is_required(self) -> None:
        with pytest.raises(ValidationError):
            EnvEndpoints(db_url="http://db:8000")

    def test_extra_fields_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            EnvEndpoints.model_validate(
                {
                    "db_url": "http://db:8000",
                    "runner_url": "http://runner:50051",
                    "this_field_does_not_exist": "x",
                }
            )

    def test_wire_shape_top_level_keys(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db:8000",
            rag_url="http://rag:8001",
            runner_url="http://runner:50051",
        )
        assert set(endpoints.model_dump().keys()) == {"db_url", "rag_url", "runner_url"}


class TestEnvEndpointsRoundTrip:
    def test_json_round_trip_with_rag(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db:8000",
            rag_url="http://rag:8001",
            runner_url="http://runner:50051",
        )
        reloaded = EnvEndpoints.model_validate_json(endpoints.model_dump_json())
        assert reloaded == endpoints

    def test_json_round_trip_without_rag(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db:8000",
            runner_url="http://runner:50051",
        )
        reloaded = EnvEndpoints.model_validate_json(endpoints.model_dump_json())
        assert reloaded == endpoints
        assert reloaded.rag_url is None
