"""Wire contract of the standalone grader service.

``GraderService.Grade`` carries nine named fields — the four the
composite dispatcher needs to drive its plug-in seams without an
in-process ``RunnerServiceImpl`` (``trial_id``, ``llm_messages_json``,
``termination_reason``, ``task_config_json``) plus three additions:

- ``judge_model_config_json`` — the judge's ``ModelConfig`` JSON so the
  grader constructs its ``LLMClient`` via the provider seam without
  inferring provider from a model name.
- ``task_description_json`` — the whole ``TaskDescription`` so the grader
  derives ``id_fields``, ``unstable_fields``, ``initial_state``, and
  ``tool_artifacts`` from one field.
- ``runner_substrate_address`` — the runner's ``SubstrateService`` gRPC
  address the ``LiveRunnerCallbackGradingSubstrate`` dials.

Fields 13 + 14 are additive proto3 strings — an unpopulated sender lands
empty strings on both, which keeps a sender populating only fields 1–7
round-tripping byte-identically:

- ``bundle_manifest_json`` (13) — ``GradeBundleManifest`` JSON so the
  grader-side dispatcher can build a ``SnapshotGradingSubstrate`` off
  the trial's grade bundle instead of dialling ``runner_substrate_address``.
- ``bundle_parts_uri`` (14) — the bundle-store URI
  (``bundle://<store-name>/<content-hash>``) the grader hands to the
  ``tolokaforge.bundle_stores`` seam to read the parts.

The post-policy agent system prompt already rides ``llm_messages_json`` as
its leading ``role=system`` message; the grader recovers it via
``split_leading_system_message``, so no separate wire field is needed.

This test locks two properties: (a) the proto shape carries all nine
fields with the field numbers and types the plan pins, and
(b) :meth:`GrpcGraderClient.grade` round-trips every field verbatim into
the injected judge callable's :class:`GradeDispatch`. If either breaks,
the grader client and the composite dispatcher would silently observe a
different payload than the one that left the producer.
"""

from __future__ import annotations

from concurrent import futures
from contextlib import contextmanager
from unittest.mock import MagicMock

import grpc
import pytest
from google.protobuf.descriptor import FieldDescriptor

from tolokaforge.core.models import Grade, GradeComponents
from tolokaforge.grader import grader_pb2, grader_pb2_grpc
from tolokaforge.grader.client import GrpcGraderClient
from tolokaforge.grader.service import GradeDispatch, GraderServiceImpl

pytestmark = pytest.mark.canonical


# -----------------------------------------------------------------------------
# Proto shape — field numbers + types
# -----------------------------------------------------------------------------


_EXPECTED_FIELDS: dict[str, tuple[int, int]] = {
    # name -> (proto field number, FieldDescriptor.CPPTYPE)
    "trial_id": (1, FieldDescriptor.CPPTYPE_STRING),
    "llm_messages_json": (2, FieldDescriptor.CPPTYPE_STRING),
    "termination_reason": (3, FieldDescriptor.CPPTYPE_STRING),
    "task_config_json": (4, FieldDescriptor.CPPTYPE_STRING),
    "judge_model_config_json": (5, FieldDescriptor.CPPTYPE_STRING),
    "task_description_json": (6, FieldDescriptor.CPPTYPE_STRING),
    "runner_substrate_address": (7, FieldDescriptor.CPPTYPE_STRING),
    "bundle_manifest_json": (13, FieldDescriptor.CPPTYPE_STRING),
    "bundle_parts_uri": (14, FieldDescriptor.CPPTYPE_STRING),
}


def test_grade_request_proto_carries_the_nine_wire_v3_fields() -> None:
    """Every wire field lands at the number and type the plan pins.

    A field-number reshuffle silently reinterprets stored data, so this
    test asserts the numbering the composite dispatcher and its client
    both rely on — moving ``task_description_json`` off field 6 would
    make a client's payload land in ``runner_substrate_address`` on the
    grader. Fields 13 + 14 are additive proto3 strings the runner-side
    producer populates when a trial has a grade bundle; the numbering
    skips 8–12 so those slots stay free for future additions without
    reshuffling.
    """
    descriptor = grader_pb2.GradeRequest.DESCRIPTOR
    got = {f.name: (f.number, f.cpp_type) for f in descriptor.fields}
    assert got == _EXPECTED_FIELDS, (
        "GradeRequest field shape drifted from the plan. Expected: "
        f"{_EXPECTED_FIELDS!r}. Got: {got!r}."
    )


def test_bundle_fields_are_plain_proto3_strings_not_optional_or_repeated() -> None:
    """The additive fields ``bundle_manifest_json`` / ``bundle_parts_uri``
    land as plain proto3 strings — unset senders land empty on the wire
    (not the ``optional`` presence bit; not a repeated shape). This
    preserves sender compatibility for callers that populate only fields
    1–7: an unpopulated ``GradeRequest`` still round-trips through both
    fields as empty strings, and downstream fail-loud "missing required
    field" checks land at the Python-side dispatcher (not at the wire
    layer).
    """
    descriptor = grader_pb2.GradeRequest.DESCRIPTOR
    fields_by_name = {f.name: f for f in descriptor.fields}
    for name in ("bundle_manifest_json", "bundle_parts_uri"):
        field = fields_by_name[name]
        assert not field.is_repeated, f"{name} must be singular, not repeated."
        assert field.containing_oneof is None, (
            f"{name} must be a plain proto3 string, not ``optional`` — "
            "the wire's empty-default idiom is what sender-compatibility "
            "relies on."
        )
        assert not field.has_presence, (
            f"{name} must carry no presence bit — unset senders land "
            "empty strings on the wire rather than an absent-field signal."
        )


# -----------------------------------------------------------------------------
# Round-trip — client payload lands on the service's GradeDispatch verbatim
# -----------------------------------------------------------------------------


@contextmanager
def _running_service(judge_fn):
    """Spin up an in-process gRPC server hosting :class:`GraderServiceImpl`.

    Uses a real (localhost) listener rather than an in-memory channel so the
    grpc-py serialisation layer is exercised — that is the thing this test
    exists to lock down for the three new v2 fields.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    grader_pb2_grpc.add_GraderServiceServicer_to_server(
        GraderServiceImpl(judge_fn=judge_fn, logger=MagicMock()), server
    )
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        client = GrpcGraderClient(grader_address=f"localhost:{port}")
        client.connect()
        try:
            yield client
        finally:
            client.close()
    finally:
        server.stop(grace=None)


def _canned_verdict() -> Grade:
    return Grade(
        binary_pass=True,
        score=1.0,
        components=GradeComponents(llm_judge=1.0),
        reasons="stub",
    )


def test_client_grade_round_trips_every_wire_field_to_dispatch() -> None:
    """Every keyword lands on :class:`GradeDispatch` verbatim.

    The judge stub captures the dispatch its ``Grade`` handler was invoked
    with; the test then asserts field-by-field equality against the client's
    inputs. If any v2 field is dropped or transposed at the grpc serialise/
    deserialise boundary — a missing field in :class:`GradeRequest`, a
    misnamed populate in :meth:`GraderServiceImpl.Grade`, a client omission
    — this assertion fires.
    """
    captured: list[GradeDispatch] = []

    def _judge_fn(dispatch: GradeDispatch) -> Grade | None:
        captured.append(dispatch)
        return _canned_verdict()

    with _running_service(_judge_fn) as client:
        result = client.grade(
            trial_id="task_id:0",
            llm_messages_json='[{"role":"user","content":"hi"}]',
            termination_reason="agent_done",
            task_config_json='{"llm_judge":{"criteria":[]}}',
            judge_model_config_json='{"provider":"litellm","name":"gpt-4"}',
            task_description_json='{"id":"task_id","tool_artifacts":{}}',
            runner_substrate_address="runner:50051",
        )

    assert result["success"] is True
    assert len(captured) == 1
    dispatch = captured[0]
    assert dispatch == GradeDispatch(
        trial_id="task_id:0",
        llm_messages_json='[{"role":"user","content":"hi"}]',
        termination_reason="agent_done",
        task_config_json='{"llm_judge":{"criteria":[]}}',
        judge_model_config_json='{"provider":"litellm","name":"gpt-4"}',
        task_description_json='{"id":"task_id","tool_artifacts":{}}',
        runner_substrate_address="runner:50051",
    )


def test_client_grade_defaults_v2_fields_to_empty_strings() -> None:
    """A caller that has not populated a v2 field lands an empty string on
    the wire — not a Python ``None`` and not the field's absence.

    The composite dispatcher rejects an empty ``task_config_json`` or
    ``task_description_json``; the empty-string default that the client
    lands on the wire when a caller omits a v2 field is what makes that
    fail-loud check reachable.
    """
    captured: list[GradeDispatch] = []

    def _judge_fn(dispatch: GradeDispatch) -> Grade | None:
        captured.append(dispatch)
        return _canned_verdict()

    with _running_service(_judge_fn) as client:
        client.grade(
            trial_id="task_id:0",
            llm_messages_json="[]",
            termination_reason="",
        )

    assert captured[0] == GradeDispatch(
        trial_id="task_id:0",
        llm_messages_json="[]",
        termination_reason="",
        task_config_json="",
        judge_model_config_json="",
        task_description_json="",
        runner_substrate_address="",
    )


def test_client_grade_signature_is_keyword_only() -> None:
    """The ``GrpcGraderClient.grade`` signature is keyword-only.

    Positional calls would silently reshuffle when a future stage adds a
    ninth wire field between two existing ones — the keyword-only shape
    guarantees the caller names each field it forwards.
    """
    import inspect

    sig = inspect.signature(GrpcGraderClient.grade)
    parameters = list(sig.parameters.values())
    # ``self`` is positional-only under `def grade(self, *, …)`; every other
    # parameter must be KEYWORD_ONLY.
    for param in parameters[1:]:
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"GrpcGraderClient.grade parameter {param.name!r} is "
            f"{param.kind!r}; expected KEYWORD_ONLY so a caller cannot "
            "positionally reshuffle wire fields."
        )
