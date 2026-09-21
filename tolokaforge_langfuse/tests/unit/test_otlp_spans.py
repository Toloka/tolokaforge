"""The projection's ingestion bodies as OTLP spans (ADR-0048, the v4 write-once layout).

The golden here is derived from ``parity_golden.json``, the event golden both repositories ship,
so the connector's copy of this test reads the same input and must produce the same output: one
converter, one span shape, whichever producer writes the trace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk")
from tolokaforge_langfuse.otlp_spans import (  # noqa: E402
    observation_bodies,
    score_events,
    span_attributes,
    spans_from_events,
    trace_facts,
)

pytestmark = pytest.mark.unit

EVENT_GOLDEN = Path(__file__).with_name("parity_golden.json")
SPAN_GOLDEN = Path(__file__).with_name("parity_span_golden.json")
ENVIRONMENT = "development"
RELEASE = "tolokaforge-0.0.0"
VERSION = "tolokaforge-0.0.0+parity"


def rows(events: list[dict]) -> list[dict]:
    """The comparable view of the converted spans: ids, parents, clocks and attributes, in
    emission order (so the golden pins that the root is written last)."""
    facts = trace_facts(events, environment=ENVIRONMENT, release=RELEASE, version=VERSION)
    out: list[dict] = []
    for event_type, body in observation_bodies(events):
        is_root = not body.get("parentObservationId")
        out.append(
            {
                "id": body["id"],
                "parent": body.get("parentObservationId"),
                "name": body.get("name"),
                "startTime": body.get("startTime"),
                "endTime": body.get("endTime"),
                "isRoot": is_root,
                "attributes": span_attributes(event_type, body, facts, is_root=is_root),
            }
        )
    return out


@pytest.fixture
def golden_events() -> list[dict]:
    return json.loads(EVENT_GOLDEN.read_text(encoding="utf-8"))


class TestTheGolden:
    def test_the_converted_spans_equal_the_golden(self, golden_events) -> None:
        assert rows(golden_events) == json.loads(SPAN_GOLDEN.read_text(encoding="utf-8"))

    def test_the_root_is_last_and_there_is_exactly_one(self, golden_events) -> None:
        converted = rows(golden_events)
        assert [row["isRoot"] for row in converted].count(True) == 1
        assert converted[-1]["isRoot"] and converted[-1]["parent"] is None

    def test_every_observation_of_the_projection_becomes_one_span(self, golden_events) -> None:
        observations = [
            e
            for e in golden_events
            if e["type"] in {"span-create", "generation-create", "event-create"}
        ]
        spans = spans_from_events(golden_events)
        assert len(spans) == len(observations)
        assert len({s.context.span_id for s in spans}) == len(spans)  # every id exactly once

    def test_scores_are_not_spans_and_travel_unchanged(self, golden_events) -> None:
        scores = score_events(golden_events)
        assert scores and all(e["type"] == "score-create" for e in scores)
        assert scores == [e for e in golden_events if e["type"] == "score-create"]


class TestTheTraceFacts:
    def test_every_span_carries_the_trace_and_the_identity(self, golden_events) -> None:
        facts = trace_facts(golden_events)
        for row in rows(golden_events):
            attributes = row["attributes"]
            assert attributes["langfuse.trace.name"] == facts.name
            assert attributes["langfuse.session.id"] == facts.session_id
            assert list(attributes["langfuse.trace.tags"]) == list(facts.tags)
            assert attributes["langfuse.environment"] == ENVIRONMENT
            assert attributes["langfuse.release"] == RELEASE
            assert attributes["langfuse.version"] == VERSION
            for key in ("task_id", "trial_index", "attempt", "run_id", "run_tag"):
                assert attributes[f"langfuse.trace.metadata.{key}"] == facts.metadata[key]

    def test_only_the_root_carries_the_whole_trace_metadata(self, golden_events) -> None:
        converted = rows(golden_events)
        root, child = converted[-1]["attributes"], converted[0]["attributes"]
        trace_keys = {k for k in root if k.startswith("langfuse.trace.metadata.")}
        assert len(trace_keys) == len(trace_facts(golden_events).metadata)
        assert {k for k in child if k.startswith("langfuse.trace.metadata.")} == {
            f"langfuse.trace.metadata.{k}"
            for k in ("task_id", "trial_index", "attempt", "run_id", "run_tag")
        }
        # the trace's input and output are the root observation's own: a receiver stores that
        # pair and keeps langfuse.trace.input / .output nowhere
        facts = trace_facts(golden_events)
        assert root["langfuse.observation.input"] == facts.input
        assert root["langfuse.observation.output"] == facts.output
        assert "langfuse.trace.input" not in root and "langfuse.trace.output" not in root

    def test_the_manifest_travels_as_a_json_string(self, golden_events) -> None:
        root = rows(golden_events)[-1]["attributes"]
        attachments = root["langfuse.trace.metadata.attachments"]
        # OTLP carries no nested value; the receiver parses a JSON string back. The golden
        # bundle is projected without a manifest, so the object it carries is the empty one.
        assert isinstance(attachments, str) and json.loads(attachments) == {}
        assert isinstance(root["langfuse.trace.metadata.attachments_skipped"], str)

    def test_no_final_row_claims_to_be_a_preview(self, golden_events) -> None:
        for row in rows(golden_events):
            assert "langfuse.observation.metadata.preview" not in row["attributes"]

    def test_the_arguments_override_the_trace_bodys_native_fields(self, golden_events) -> None:
        facts = trace_facts(golden_events, environment="other")
        assert facts.environment == "other"
        assert trace_facts(golden_events).environment == ENVIRONMENT


class TestTheRules:
    def _events(self, *bodies: dict) -> list[dict]:
        trace = {
            "type": "trace-create",
            "body": {
                "id": "a" * 32,
                "name": "label/T-1",
                "sessionId": "s",
                "tags": ["harness:tolokaforge"],
                "metadata": {"task_id": "T-1", "status": "completed", "nested": {"a": 1}},
                "environment": "development",
            },
        }
        return [trace, *({"type": "span-create", "body": b} for b in bodies)]

    def test_a_nested_observation_metadata_value_is_a_json_string(self) -> None:
        events = self._events(
            {"id": "b" * 16, "traceId": "a" * 32, "metadata": {"list": [1, 2], "flag": True}}
        )
        attributes = rows(events)[0]["attributes"]
        assert attributes["langfuse.observation.metadata.list"] == "[1, 2]"
        assert attributes["langfuse.observation.metadata.flag"] is True

    def test_an_absent_metadata_value_travels_as_the_projections_own_none(self) -> None:
        events = self._events({"id": "b" * 16, "traceId": "a" * 32, "metadata": {"reason": None}})
        assert rows(events)[0]["attributes"]["langfuse.observation.metadata.reason"] == "none"

    def test_the_trace_metadata_wins_over_an_observation_key_of_the_same_name(self) -> None:
        events = self._events(
            {"id": "b" * 16, "traceId": "a" * 32, "metadata": {"status": "running", "own": 1}}
        )
        root = rows(events)[0]["attributes"]  # no parent: this body is the root
        assert root["langfuse.trace.metadata.status"] == "completed"
        assert "langfuse.observation.metadata.status" not in root
        assert root["langfuse.observation.metadata.own"] == 1

    def test_a_manifest_on_the_root_survives_as_the_same_document(self) -> None:
        manifest = {
            "trajectory.yaml": {"media_id": "m1", "sha256": "ab", "bytes": 12},
            "grade.yaml": {"media_id": "m2", "sha256": "cd", "bytes": 34},
        }
        events = [
            {
                "type": "trace-create",
                "body": {
                    "id": "a" * 32,
                    "metadata": {"attachments": manifest, "attachments_complete": True},
                },
            },
            {"type": "span-create", "body": {"id": "b" * 16, "traceId": "a" * 32}},
        ]
        attributes = rows(events)[0]["attributes"]
        assert json.loads(attributes["langfuse.trace.metadata.attachments"]) == manifest
        assert attributes["langfuse.trace.metadata.attachments_complete"] is True

    def test_usage_and_cost_travel_as_json_strings(self) -> None:
        events = self._events(
            {
                "id": "b" * 16,
                "traceId": "a" * 32,
                "usageDetails": {"input": 1, "output": 2},
                "costDetails": {"total": 0.5},
            }
        )
        attributes = rows(events)[0]["attributes"]
        assert json.loads(attributes["langfuse.observation.usage_details"]) == {
            "input": 1,
            "output": 2,
        }
        assert json.loads(attributes["langfuse.observation.cost_details"]) == {"total": 0.5}

    def test_an_error_level_makes_the_span_an_error(self) -> None:
        events = self._events(
            {"id": "b" * 16, "traceId": "a" * 32, "level": "ERROR", "statusMessage": "boom"}
        )
        span = spans_from_events(events)[0]
        assert span.status.status_code.name == "ERROR"
        assert span.attributes["langfuse.observation.status_message"] == "boom"

    def test_an_event_without_an_end_ends_when_it_started(self) -> None:
        events = [
            {
                "type": "event-create",
                "body": {
                    "id": "c" * 16,
                    "traceId": "a" * 32,
                    "parentObservationId": "b" * 16,
                    "startTime": "2026-09-17T09:00:00+00:00",
                },
            }
        ]
        span = spans_from_events(events)[0]
        assert span.start_time == span.end_time
        assert span.attributes["langfuse.observation.type"] == "event"

    def test_an_event_list_without_a_trace_body_still_converts(self) -> None:
        events = [{"type": "span-create", "body": {"id": "b" * 16, "traceId": "a" * 32}}]
        span = spans_from_events(events, environment="development")[0]
        assert span.attributes["langfuse.environment"] == "development"
        assert "langfuse.trace.name" not in span.attributes
