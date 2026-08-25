"""Reference parity harness for the ``runner_rpc`` and ``grader_rpc`` legs.

Loads one parity pack from disk, boots an in-process
:class:`RunnerServiceImpl` + :class:`SubstrateServicer` gRPC pair, then
drives each grading leg against the same trial context. Both legs return
a wire-shaped ``Grade`` proto; :func:`serialise_grade` projects either
proto type onto a canonical dict a byte-parity assertion can run against.

Pack layout on disk (per pack directory):

* ``task.yaml`` — :class:`~tolokaforge.runner.models.TaskDescription` fields
  (task_id, name, category, description, initial_state, adapter_type,
  system_prompt).
* ``grading.yaml`` — :class:`~tolokaforge.runner.models.RunnerGradingConfig`.
* ``trial.yaml`` — wire fields the harness drives grading with: ``trial_id``,
  ``termination_reason``, ``agent_system_prompt``, ``llm_messages``,
  ``judge_model_config``.
* ``parity.yaml`` — accepted divergences declaration, optional ``judge_script``
  (scripted GenerationResult sequence for packs exercising ``llm_judge``),
  optional ``db_probe_rows`` mapping (deterministic rows the harness serves
  from :func:`tolokaforge.runner.grading._fetch_probe_rows` for packs
  exercising ``state_checks.db_probes``), optional refusal-mode contract.
* ``expected_grade.json`` — the committed baseline; rewritten in place by
  the canonical test when it runs under ``--refresh-baselines``.

Two proto message types reach :func:`serialise_grade` — ``runner_pb2.Grade``
from the runner leg and ``grader_pb2.Grade`` from the grader leg — with
structurally identical descriptors. The canonical-dict projection collapses
them into the same JSON so parity is asserted at the observable-wire layer,
not at the proto-type layer.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml
from google.protobuf import json_format

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig, ToolCall
from tolokaforge.core.trial_grader import GradingFailedError
from tolokaforge.grader import grader_pb2
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.grader.service import GradeDispatch, _grade_to_wire
from tolokaforge.runner import (
    add_RunnerServiceServicer_to_server,
    add_SubstrateServiceServicer_to_server,
    runner_pb2,
)
from tolokaforge.runner import grading as runner_grading
from tolokaforge.runner.models import (
    ResetTrialResponse,
    RestoreSnapshotResponse,
    RunnerGradingConfig,
    SnapshotResponse,
    StableStateResponse,
    StateResponse,
    TaskDescription,
)
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime
from tolokaforge.runner.substrate_service import SubstrateServicer

_CUSTOM_CHECKS_FILE = "checks.py"
"""Well-known name for a pack's custom-checks module.

A pack shipping this file wires it into both legs: base64-encoded onto
:attr:`TaskDescription.tool_artifacts` for the grader leg (extracted to a
temp dir at grade time) and served through :attr:`RunnerServiceImpl._artifact_dirs`
directly out of the pack directory for the runner leg. Both legs then load
the same source under the same relative path."""


@dataclass(frozen=True)
class ParityPack:
    """One parity pack, wire fields and metadata pre-decoded.

    Fields:

    * ``directory`` — the pack root, where ``expected_grade.json`` lives.
    * ``task_description`` — validated :class:`TaskDescription` (task.yaml with
      grading.yaml folded onto ``grading``).
    * ``grading_config`` — the :class:`RunnerGradingConfig` from grading.yaml,
      also available on ``task_description.grading``; kept as its own field
      because :meth:`GraderCompositeDispatch.grade` receives it serialised
      via the wire ``task_config_json`` before deserialising back.
    * ``trial_id`` — canonical ``{task_id}:{trial_index}``.
    * ``llm_messages_json`` — the transcript in the runner's OpenAI-shape
      JSON string; the same payload lands on both wires.
    * ``termination_reason`` — enum-name string; ``""`` when the caller
      reports none.
    * ``agent_system_prompt`` — post-policy authoritative system prompt.
    * ``judge_model_config`` — the judge's :class:`ModelConfig`, or ``None``
      when the pack declares no ``llm_judge`` block.
    * ``judge_script`` — deterministic scripted responses for the judge's
       :class:`LLMClient` — a list of ``str`` (assistant text) or a list of
      ``[(tool_name, arguments_dict), ...]`` per turn. Empty when the pack
      does not exercise ``llm_judge``.
    * ``accepted_divergences`` — declared parity carve-outs; the placeholder
      pack uses none.
    * ``refusal_mode`` — when true, the grader leg is expected to raise
      :class:`GradingFailedError` with ``expected_error_fragment`` in the
      message rather than produce a Grade.
    * ``expected_error_fragment`` — substring the raised error must contain
      under ``refusal_mode``; empty otherwise.
    * ``db_probe_rows`` — deterministic rows served to a pack's
      ``state_checks.db_probes`` seam, keyed by probe name. Each list is
      the rows :func:`tolokaforge.runner.grading._fetch_probe_rows` returns
      when the probe of that name runs; both legs draw from the same
      mapping. Empty for packs that declare no ``db_probes`` block, which
      is every non-``db_probes`` pack.
    * ``has_custom_checks_file`` — whether the pack ships a top-level
      :file:`checks.py`. The loader mirrors it onto both legs (base64 into
      ``task_description.tool_artifacts`` for the grader; a pack-directory
      seed of ``runner._artifact_dirs[trial_id]`` for the runner).
    """

    directory: Path
    task_description: TaskDescription
    grading_config: RunnerGradingConfig
    trial_id: str
    llm_messages_json: str
    termination_reason: str
    agent_system_prompt: str
    judge_model_config: ModelConfig | None
    judge_script: list[Any]
    accepted_divergences: tuple[str, ...]
    refusal_mode: bool
    expected_error_fragment: str
    db_probe_rows: dict[str, list[dict[str, Any]]] = dataclass_field(default_factory=dict)
    has_custom_checks_file: bool = False


def load_parity_pack(pack_dir: Path) -> ParityPack:
    """Read the four YAML files (plus optional judge script) into a
    :class:`ParityPack`. Validation is Pydantic ``extra=forbid`` on
    :class:`TaskDescription` / :class:`RunnerGradingConfig`, so a field
    the pack authored but nothing reads fails at load time.

    A pack shipping a top-level :file:`checks.py` gets it base64-encoded
    onto :attr:`TaskDescription.tool_artifacts` so the grader leg's
    :func:`extract_tool_artifacts` materialises the same source the
    runner leg reads out of ``pack_dir``.
    """
    task_data = yaml.safe_load((pack_dir / "task.yaml").read_text())
    grading_data = yaml.safe_load((pack_dir / "grading.yaml").read_text())
    trial_data = yaml.safe_load((pack_dir / "trial.yaml").read_text())
    parity_data = yaml.safe_load((pack_dir / "parity.yaml").read_text()) or {}

    grading_config = RunnerGradingConfig.model_validate(grading_data)
    task_payload = dict(task_data)
    task_payload["grading"] = grading_config.model_dump()

    checks_path = pack_dir / _CUSTOM_CHECKS_FILE
    has_custom_checks_file = checks_path.is_file()
    if has_custom_checks_file:
        artifacts = dict(task_payload.get("tool_artifacts") or {})
        artifacts[_CUSTOM_CHECKS_FILE] = base64.b64encode(checks_path.read_bytes()).decode("ascii")
        task_payload["tool_artifacts"] = artifacts

    task_description = TaskDescription.model_validate(task_payload)

    judge_model_config_data = trial_data.get("judge_model_config")
    judge_model_config = (
        ModelConfig.model_validate(judge_model_config_data)
        if judge_model_config_data is not None
        else None
    )

    llm_messages = trial_data.get("llm_messages", [])
    llm_messages_json = json.dumps(llm_messages)

    judge_script = _normalise_judge_script(parity_data.get("judge_script", []))
    accepted = tuple(parity_data.get("accepted_divergences", []))
    refusal_mode = bool(parity_data.get("refusal_mode", False))
    expected_error_fragment = str(parity_data.get("expected_error_fragment", ""))
    db_probe_rows = _normalise_db_probe_rows(parity_data.get("db_probe_rows", {}))

    return ParityPack(
        directory=pack_dir,
        task_description=task_description,
        grading_config=grading_config,
        trial_id=str(trial_data["trial_id"]),
        llm_messages_json=llm_messages_json,
        termination_reason=str(trial_data.get("termination_reason", "")),
        agent_system_prompt=str(trial_data.get("agent_system_prompt", "")),
        judge_model_config=judge_model_config,
        judge_script=judge_script,
        accepted_divergences=accepted,
        refusal_mode=refusal_mode,
        expected_error_fragment=expected_error_fragment,
        db_probe_rows=db_probe_rows,
        has_custom_checks_file=has_custom_checks_file,
    )


def _normalise_db_probe_rows(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Coerce the authored mapping into ``{probe_name: [row_dict, ...]}``.

    A pack that declares no db_probes at all writes no entry and lands on
    an empty dict here; a pack declaring the seam but no scripted rows
    for a given probe surfaces a KeyError at monkeypatch time, which is
    the loud shape a missing script should have — the two legs would
    otherwise diverge on real network errors between runs.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"parity.yaml db_probe_rows must be a mapping, got {type(raw).__name__}")
    normalised: dict[str, list[dict[str, Any]]] = {}
    for probe_name, rows in raw.items():
        if not isinstance(rows, list):
            raise ValueError(
                f"parity.yaml db_probe_rows[{probe_name!r}] must be a list of row dicts"
            )
        normalised[str(probe_name)] = [dict(row) for row in rows]
    return normalised


def _normalise_judge_script(raw: list[Any]) -> list[Any]:
    """Translate the authored ``judge_script`` (YAML) into the shape
    :class:`_ScriptedClient` consumes: each entry is either a plain
    string (assistant text) or a list of ``(tool_name, arguments)``
    tuples for a tool-call turn.
    """
    normalised: list[Any] = []
    for step in raw:
        if isinstance(step, str):
            normalised.append(step)
            continue
        if isinstance(step, dict) and "text" in step:
            normalised.append(str(step["text"]))
            continue
        if isinstance(step, list):
            calls: list[tuple[str, dict[str, Any]]] = []
            for call in step:
                assert isinstance(call, dict), f"tool-call step must be mapping, got {call!r}"
                assert (
                    "name" in call and "arguments" in call
                ), f"tool-call step must carry name+arguments, got {call!r}"
                calls.append((str(call["name"]), dict(call["arguments"])))
            normalised.append(calls)
            continue
        raise ValueError(f"unrecognised judge_script step shape: {step!r}")
    return normalised


class _ScriptedClient:
    """Deterministic scripted stand-in for
    :class:`tolokaforge.core.llm.client.LLMClient`.

    Both parity legs share this client via a monkeypatched constructor at
    :mod:`tolokaforge.core.grading.default_judge_model_provider`. Each leg's
    :class:`GraderCompositeDispatch` / runner-side judge picks up its own
    fresh scripted client instance drawing from a copy of the script list.
    Two calls to :meth:`generate` are exhausted in order; a third yields the
    ``(exhausted)`` sentinel so a runaway loop surfaces distinctively rather
    than deadlocking against an empty queue.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self._i = 0

    def generate(
        self,
        system,  # noqa: ARG002 — protocol arg, unused by the script
        messages,  # noqa: ARG002
        tools,  # noqa: ARG002
        tool_choice="auto",  # noqa: ARG002
        observation=None,  # noqa: ARG002
    ) -> GenerationResult:
        if self._i >= len(self._script):
            return GenerationResult(text="(exhausted)", tool_calls=[], usage=Usage())
        step = self._script[self._i]
        self._i += 1
        if isinstance(step, str):
            return GenerationResult(text=step, tool_calls=[], usage=Usage())
        tool_calls = [
            ToolCall(id=f"call_{self._i}_{j}", name=name, arguments=args)
            for j, (name, args) in enumerate(step)
        ]
        return GenerationResult(
            text="",
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.001,
        )

    def classify_loop_error(self, exc: Exception):
        from tolokaforge.core.loop import classify_loop_error

        return classify_loop_error(exc, ())


class _FakeDBServiceClient:
    """Deterministic DB stand-in that serves the pack's declared tables as
    both the RAW final view and the STABLE view.

    A parity pack under this harness models a stationary trial: the state
    the substrate reports at grade time equals the pack's declared
    ``initial_state.tables``. Every substrate read (jsonpath, db-probes,
    judge state-diff) draws from the same authored bytes, so a divergence
    between the two legs is a divergence in dispatch code — not in the
    stub's per-call bookkeeping.

    The hash-grading surface (:meth:`get_stable_hash`, :meth:`create_snapshot`,
    :meth:`restore_snapshot`, :meth:`reset_trial`) is a no-op family that
    keeps the runner leg's :meth:`_execute_hash_grading` on its happy path
    for a stationary trial: the trial and golden hashes are identical
    constants, so hash grading resolves to ``hash_match=True`` without a
    real snapshot / reset backend. Grader-leg hash grading is out of
    reach — the composite dispatcher refuses ``hash_enabled`` up front —
    so only the runner leg reaches these methods on any pack.
    """

    _STABLE_HASH = "parity-harness-stable-hash"

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._data = dict(tables)

    async def get_state(
        self,
        trial_id: str,  # noqa: ARG002
        tables: list[str] | None = None,  # noqa: ARG002
    ) -> StateResponse:
        return StateResponse(
            data=dict(self._data), version=1, full_hash="full", stable_hash="stable"
        )

    async def get_stable_state(self, trial_id: str) -> StableStateResponse:  # noqa: ARG002
        return StableStateResponse(
            data=dict(self._data), version=1, stable_hash="stable", filtered_fields=[]
        )

    async def get_stable_hash(
        self,
        trial_id: str,  # noqa: ARG002
        *,
        numeric_string_fields: Any = None,  # noqa: ARG002
    ) -> str:
        return self._STABLE_HASH

    async def create_snapshot(
        self,
        trial_id: str,  # noqa: ARG002
        name: str,
    ) -> SnapshotResponse:
        return SnapshotResponse(status="ok", snapshot_name=name, version=1, hash=self._STABLE_HASH)

    async def restore_snapshot(
        self,
        trial_id: str,  # noqa: ARG002
        name: str,
    ) -> RestoreSnapshotResponse:
        return RestoreSnapshotResponse(
            status="ok", restored_from=name, version=1, hash=self._STABLE_HASH
        )

    async def reset_trial(self, trial_id: str) -> ResetTrialResponse:  # noqa: ARG002
        return ResetTrialResponse(status="ok", version=1, hash=self._STABLE_HASH)

    async def health_check(self) -> Any:
        raise AssertionError("parity harness does not exercise health_check")

    async def close(self) -> None:
        return None


class _NullGrpcContext:
    """Minimum :class:`grpc.ServicerContext` stand-in — the ``GradeTrial``
    servicer needs only an object; none of its methods are exercised."""

    def set_code(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass

    def set_details(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass


@dataclass
class _RunningRunner:
    runner: RunnerServiceImpl
    substrate_address: str
    trial_context: TrialContextRuntime


@contextmanager
def _running_runner(pack: ParityPack) -> Iterator[_RunningRunner]:
    """Boot :class:`RunnerServiceImpl` + :class:`SubstrateServicer` on an
    in-process gRPC server and register the pack's :class:`TrialContextRuntime`.

    The runner leg calls :meth:`RunnerServiceImpl.GradeTrial` directly on
    the returned instance; the grader leg dials ``substrate_address``
    through a :class:`LiveRunnerCallbackGradingSubstrate` its
    :class:`GraderCompositeDispatch` constructs internally. Both legs
    share the same :class:`_FakeDBServiceClient` so DB reads compare like
    for like.
    """
    tables = dict(pack.task_description.initial_state.tables)
    fake_db = _FakeDBServiceClient(tables=tables)
    runner = RunnerServiceImpl(db_client=fake_db)  # type: ignore[arg-type]
    trial_context = TrialContextRuntime(
        trial_id=pack.trial_id,
        task_description=pack.task_description,
        judge_model_config=pack.judge_model_config,
    )
    runner.trials[pack.trial_id] = trial_context
    if pack.has_custom_checks_file:
        # Runner-side ``_grade_custom_checks`` reads ``checks.py`` out of
        # ``self._artifact_dirs.get(trial_id)``; the grader leg extracts
        # ``task_description.tool_artifacts`` to its own temp dir at grade
        # time. Both legs land on the same source under the same relative
        # path.
        runner._artifact_dirs[pack.trial_id] = pack.directory

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_RunnerServiceServicer_to_server(runner, server)
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        yield _RunningRunner(
            runner=runner,
            substrate_address=f"localhost:{port}",
            trial_context=trial_context,
        )
    finally:
        server.stop(grace=None)
        if runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)


def _install_scripted_client(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
    """Route :class:`LiteLLMJudgeModelProvider`'s
    :class:`~tolokaforge.core.llm.client.LLMClient` construction to a
    fresh :class:`_ScriptedClient`. Each call to the constructor gets its
    own copy of the script, so the runner and grader legs each run a
    complete judge dispatch against the same deterministic responses.
    """
    monkeypatch.setattr(
        "tolokaforge.core.grading.default_judge_model_provider.LLMClient",
        lambda *args, **kwargs: _ScriptedClient(script),
    )


def _install_db_probe_rows(
    monkeypatch: pytest.MonkeyPatch,
    pack: ParityPack,
) -> None:
    """Route :func:`tolokaforge.runner.grading._fetch_probe_rows` to
    pack-declared row sets instead of opening a live postgres connection.

    Both legs' ``state_check_backends["db_probes"]`` is
    :class:`~tolokaforge.core.grading.default_state_check_backends.DbProbesStateCheckBackend`,
    whose :meth:`query` wraps :func:`evaluate_db_probes` — the sole caller
    of :func:`_fetch_probe_rows`. Monkeypatching that seam keeps the
    surrounding backend logic (per-probe ``expect`` JSONPath evaluation,
    pass/fail accounting, reasons composition) under test symmetrically
    on both legs.

    Rows are keyed by SQL ``query`` string: :func:`_fetch_probe_rows`
    receives ``(dsn, query)`` and cannot see the probe name, so the
    pack authors the same ``query`` verbatim in both ``grading.yaml``
    and ``parity.yaml`` and the loader translates ``db_probe_rows`` from
    probe-name keys into query-string keys here.
    """
    query_to_rows: dict[str, list[dict[str, Any]]] = {}
    state_checks = pack.grading_config.state_checks
    declared_probes = state_checks.db_probes if state_checks else []
    for probe in declared_probes:
        rows = pack.db_probe_rows.get(probe.name)
        if rows is None:
            raise KeyError(
                f"pack {pack.directory.name!r} declares db_probe {probe.name!r} but "
                f"parity.yaml carries no db_probe_rows[{probe.name!r}] script — the "
                "canonical lane cannot resolve the probe deterministically without one"
            )
        query_to_rows[probe.query] = rows

    async def fake_fetch(dsn: str, query: str) -> list[dict[str, Any]]:  # noqa: ARG001
        try:
            return list(query_to_rows[query])
        except KeyError as exc:
            raise AssertionError(
                f"parity harness has no scripted rows for query {query!r} — "
                "either grading.yaml drifted from parity.yaml or the seam "
                "issued a query the pack did not declare"
            ) from exc

    monkeypatch.setattr(runner_grading, "_fetch_probe_rows", fake_fetch)


def _build_grade_dispatch(pack: ParityPack, *, substrate_address: str) -> GradeDispatch:
    """Assemble the wire :class:`GradeDispatch` the grader service receives.

    Every field lands on the wire :meth:`GraderServiceImpl.Grade` receives
    from a gRPC request; the harness stays symmetric with the shipped
    request-handling path by serialising each Pydantic model to its JSON
    representation.
    """
    judge_json = (
        pack.judge_model_config.model_dump_json() if pack.judge_model_config is not None else ""
    )
    return GradeDispatch(
        trial_id=pack.trial_id,
        llm_messages_json=pack.llm_messages_json,
        termination_reason=pack.termination_reason,
        task_config_json=pack.grading_config.model_dump_json(),
        judge_model_config_json=judge_json,
        task_description_json=pack.task_description.model_dump_json(),
        runner_substrate_address=substrate_address,
        agent_system_prompt=pack.agent_system_prompt,
    )


def run_via_runner_rpc(
    pack: ParityPack,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> runner_pb2.Grade:
    """Drive :meth:`RunnerServiceImpl.GradeTrial` and return the resulting
    :class:`runner_pb2.Grade`.

    Fails loud on ``response.success == False`` — this leg carries the
    baseline the parity gate anchors on, so a runner-side failure is a
    real regression the harness must surface rather than smooth over.
    """
    _install_scripted_client(monkeypatch, pack.judge_script)
    if pack.db_probe_rows:
        _install_db_probe_rows(monkeypatch, pack)
    with _running_runner(pack) as ctx:
        response = ctx.runner.GradeTrial(
            runner_pb2.GradeTrialRequest(
                trial_id=pack.trial_id,
                llm_messages_json=pack.llm_messages_json,
                termination_reason=pack.termination_reason,
            ),
            _NullGrpcContext(),
        )
        assert response.success, f"runner_rpc leg failed: {response.error}"
        return response.grade


def run_via_grader_rpc(
    pack: ParityPack,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> grader_pb2.Grade:
    """Drive :class:`GraderCompositeDispatch` end-to-end and return the
    :class:`grader_pb2.Grade` shipped over the wire.

    The pack's wire dispatch fields (task_config_json,
    task_description_json, judge_model_config_json, runner_substrate_address,
    agent_system_prompt) round-trip through Pydantic JSON so the harness
    exercises the deserialisation the standalone grader-service caller
    would trigger.
    """
    _install_scripted_client(monkeypatch, pack.judge_script)
    if pack.db_probe_rows:
        _install_db_probe_rows(monkeypatch, pack)
    with _running_runner(pack) as ctx:
        dispatch = _build_grade_dispatch(pack, substrate_address=ctx.substrate_address)
        composite = GraderCompositeDispatch(
            logger=StructuredLogger(name="parity-gate-grader-rpc")  # type: ignore[arg-type]
        )
        grade = composite.grade(dispatch)
        assert grade is not None, "grader_rpc leg produced no verdict"
        return _grade_to_wire(grade)


def assert_grader_rpc_refuses(
    pack: ParityPack,
    expected_error_fragment: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert :class:`GraderCompositeDispatch.grade` raises
    :class:`GradingFailedError` whose message contains
    ``expected_error_fragment``.

    Fails when the dispatch returns a :class:`Grade` (no refusal), when a
    different exception is raised, or when the raised message does not
    carry the expected fragment. Covers the refusal branch a hash-enabled
    pack lands on.
    """
    _install_scripted_client(monkeypatch, pack.judge_script)
    if pack.db_probe_rows:
        _install_db_probe_rows(monkeypatch, pack)
    with _running_runner(pack) as ctx:
        dispatch = _build_grade_dispatch(pack, substrate_address=ctx.substrate_address)
        composite = GraderCompositeDispatch(
            logger=StructuredLogger(name="parity-gate-grader-rpc")  # type: ignore[arg-type]
        )
        with pytest.raises(GradingFailedError) as excinfo:
            composite.grade(dispatch)
        assert expected_error_fragment in str(excinfo.value), (
            f"grader refused but the message does not carry the declared fragment "
            f"{expected_error_fragment!r}: {str(excinfo.value)!r}"
        )


def serialise_grade(grade: runner_pb2.Grade | grader_pb2.Grade) -> str:
    """Canonical JSON projection shared by both wire types.

    ``preserving_proto_field_name=True`` keeps the ``snake_case`` field
    names that appear in the .proto declarations, so a
    :func:`json_format.MessageToDict` output stays diffable against a
    hand-authored baseline. ``always_print_fields_with_no_presence=True``
    preserves scalar defaults, so a component that scored ``0.0`` and a
    component that never ran remain distinguishable in the JSON (the
    latter carries the ``-1.0`` sentinel).

    Floats normalise through ``%.6g`` (six significant digits) so a
    numerically-equivalent double rendered with a different string
    representation across proto builds still lands as byte-identical.
    """
    projected = json_format.MessageToDict(
        grade,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
    normalised = _normalise_optional_components(projected)
    normalised = _normalise_floats(normalised)
    return json.dumps(normalised, sort_keys=True, indent=2)


_OPTIONAL_COMPONENT_DEFAULTS: dict[str, float] = {"trace_checks": -1.0}
"""Optional ``GradeComponents`` fields the projection materialises with a
sentinel default when the wire carries no presence.

The wire's ``trace_checks`` field is proto3-``optional`` (:file:`runner.proto`
lines 391-400): "Absent means not evaluated; present -1.0 also means not
evaluated." The runner leg populates it with -1.0 explicitly; the grader
leg leaves it unset when the pack declares no trace-checks block. Both
encodings mean "not evaluated"; the projection collapses them onto the
runner leg's sentinel so byte parity holds at the canonical-dict layer.
"""


def _normalise_optional_components(projected: dict[str, Any]) -> dict[str, Any]:
    """Materialise semantically-equivalent absent-vs-sentinel encodings.

    Only ``GradeComponents`` optional fields the two legs encode
    differently — anything else round-trips as-is.
    """
    components = projected.get("components")
    if not isinstance(components, dict):
        return projected
    for field, default in _OPTIONAL_COMPONENT_DEFAULTS.items():
        components.setdefault(field, default)
    return projected


def _normalise_floats(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalise_floats(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalise_floats(child) for child in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


REFRESH_BASELINES_OPTION = "--refresh-baselines"
"""Pytest addopt name canonical parity tests query.

Registered under :mod:`tests.conftest`; scoping the flag globally rather
than under ``tests/canonical/conftest.py`` lets a mixed collection
(``tests/canonical/`` + a scratch reproducer under ``tests/unit/``)
still consume the flag through :attr:`pytest.Config.getoption`.
"""


def should_refresh_baselines(config: pytest.Config) -> bool:
    """Whether the canonical parity tests should rewrite their baselines.

    Kept as a helper so the individual tests never spell the option name
    inline — a future rename of the addopt updates one call site.
    """
    return bool(config.getoption(REFRESH_BASELINES_OPTION))


def write_baseline(pack: ParityPack, serialised: str) -> None:
    """Rewrite the pack's committed baseline to ``serialised``, terminating
    with a single trailing newline so the file always ends cleanly for
    diffs and ``git`` porcelain output.
    """
    baseline_path = pack.directory / "expected_grade.json"
    baseline_path.write_text(serialised + "\n", encoding="utf-8")


def read_baseline(pack: ParityPack) -> str:
    """Return the pack's committed baseline as the exact string
    :func:`serialise_grade` produces (with the trailing newline stripped)."""
    baseline_path = pack.directory / "expected_grade.json"
    return baseline_path.read_text(encoding="utf-8").rstrip("\n")


def refresh_or_assert_baseline(
    request: pytest.FixtureRequest,
    pack: ParityPack,
    *,
    runner_serialised: str,
    grader_serialised: str,
) -> None:
    """Under ``--refresh-baselines`` rewrite the committed baseline from
    the runner leg's output and skip the equality assertions; otherwise
    assert both legs match the baseline byte-for-byte.

    The runner leg anchors the baseline because the runner-RPC path is
    the shipping default; the grader leg's baseline parity is the
    assertion the parity gate ships to catch.

    A failing equality assertion carries the ``components`` slots that
    diverged in its message — the ``GradeComponents`` wire fields
    :func:`components_diff` returns — so an operator reading the pytest
    report reads the seam that regressed before opening the diff.

    ``pytest.skip`` after a refresh is the "stops before asserting"
    sentinel — the assertions below never run in refresh mode.
    """
    if should_refresh_baselines(request.config):
        write_baseline(pack, runner_serialised)
        pytest.skip("baselines refreshed")
    baseline = read_baseline(pack)
    if runner_serialised != baseline:
        raise AssertionError(
            _build_diverging_components_message(
                leg="runner_rpc",
                serialised=runner_serialised,
                baseline=baseline,
                suffix="run pytest with --refresh-baselines to accept the new baseline.",
            )
        )
    if grader_serialised != baseline:
        raise AssertionError(
            _build_diverging_components_message(
                leg="grader_rpc",
                serialised=grader_serialised,
                baseline=baseline,
                suffix="the two grading legs no longer produce byte-identical Grade output.",
            )
        )


def components_diff(a_serialised: str, b_serialised: str) -> list[str]:
    """Return the ``GradeComponents`` slots whose scores differ between two
    serialised Grade JSONs.

    Reads the top-level ``components`` mapping of each; a slot appears in
    the result when the two mappings disagree at that key (either
    different scores or one side missing). Slots the callers exercise are
    ``state_checks``, ``transcript_rules``, ``trace_checks``, ``llm_judge``,
    and ``custom_checks``; other top-level fields (``score``, ``reasons``,
    detail lists) may diverge alongside a component but are not reported
    here — regression tests exercise this at the wire-field granularity
    the operator reads, not the aggregate.
    """
    a = json.loads(a_serialised).get("components") or {}
    b = json.loads(b_serialised).get("components") or {}
    keys = sorted(set(a) | set(b))
    return [key for key in keys if a.get(key) != b.get(key)]


def _build_diverging_components_message(
    *,
    leg: str,
    serialised: str,
    baseline: str,
    suffix: str,
) -> str:
    diverging = components_diff(serialised, baseline)
    if diverging:
        component_list = ", ".join(diverging)
        return (
            f"{leg} leg diverged from the committed baseline on "
            f"components [{component_list}]; {suffix}"
        )
    return (
        f"{leg} leg diverged from the committed baseline outside the "
        f"components map (score / reasons / detail lists differ but every "
        f"component score matches); {suffix}"
    )


__all__ = [
    "REFRESH_BASELINES_OPTION",
    "ParityPack",
    "assert_grader_rpc_refuses",
    "components_diff",
    "load_parity_pack",
    "read_baseline",
    "refresh_or_assert_baseline",
    "run_via_grader_rpc",
    "run_via_runner_rpc",
    "serialise_grade",
    "should_refresh_baselines",
    "write_baseline",
]
