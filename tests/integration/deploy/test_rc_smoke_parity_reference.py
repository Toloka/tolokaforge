"""Nightly RC-smoke parity gate — the 10 reference packs over the real gRPC wire.

Boots ``deploy/standalone/docker-compose.yaml`` pinned to
``TOLOKAFORGE_SMOKE_IMAGE_TAG`` and drives every pack under
``tests/canonical/grader_parity_baselines/`` through the freshly-published
runner + grader images. Two assertion tiers, honestly split by whether the
pack exercises ``llm_judge``:

* **Deterministic tier** — the six no-judge packs assert byte-identical
  ``Grade`` against the committed baseline (canonical-dict projection via
  :func:`~tests.utils.grader_parity_harness.serialise_grade`). Byte parity
  is the shipped guarantee this gate ships.

* **Wire-shape tier** — the three ``llm_judge`` packs run WITHOUT an LLM key,
  so the judge dispatch errors intentionally. Assertion shape: the grader
  produces ``GradeResponse(success=true)`` whose Grade carries
  ``judge_status == JUDGE_STATUS_ERRORED``, a ``JUDGE ERRORED`` segment in
  ``reasons``, and non-judge components byte-matching the baseline's
  non-judge components. A ``success=false`` on a judge pack is a regression
  this gate is here to catch — the assertion refuses it.

* **Hash-refusal** — the ``hash_and_all_four`` pack rides its own test:
  the grader refuses (``success=false``) with the documented "cannot execute
  hash-based grading" fragment. Refusal precedes any judge dial so the same
  contract holds whether or not an LLM key is present.

The pack corpus is the SAME directory the canonical parity test reads —
``tests/canonical/grader_parity_baselines/``. No fork: a baseline drift in
one lane surfaces on the other automatically.

The ``state_checks_db_probes_only`` pack points its ``db_probes.dsn`` at an
``app-db`` postgres that is not part of the standalone compose stack, so
its byte-parity is out of reach for real containers; canonical parity via
the harness's monkeypatched ``_fetch_probe_rows`` covers it. The
integration lane skips it explicitly with that reason.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import grpc
import pytest

from tests.integration.deploy.conftest import (
    StackHandle,
    compose,
    pull_published,
    smoke_image_tag,
)
from tests.utils.grader_parity_harness import (
    ParityPack,
    load_parity_pack,
    read_baseline,
    serialise_grade,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.slow,
    pytest.mark.skipif(
        smoke_image_tag() is None,
        reason="TOLOKAFORGE_SMOKE_IMAGE_TAG unset — rc-smoke runs only against pushed images",
    ),
]

_BASELINES_ROOT = Path(__file__).resolve().parents[2] / "canonical" / "grader_parity_baselines"

# Import-time invariant: the RC-smoke lane and the canonical parity test must
# read the SAME pack corpus. A rename or move fails collection here rather
# than as a silent skip mid-CI.
assert _BASELINES_ROOT.is_dir(), (
    f"RC-smoke parity gate expects the pack corpus at {_BASELINES_ROOT!r} — the "
    "canonical parity test reads from the same directory; corpus fork forbidden"
)

# Six no-judge packs anchor byte-identical parity over real containers.
_DETERMINISTIC_PACK_IDS: tuple[str, ...] = (
    "state_checks_jsonpath_only",
    "state_checks_db_probes_only",
    "transcript_rules_only",
    "trace_checks_heavy",
    "custom_checks_only",
    "state_plus_transcript",
)

# Three judge packs anchor the keyless wire-shape tier. ``hash_and_all_four``
# has its own dedicated test — its refusal precedes any judge dial and the
# assertions target ``GradeResponse.error`` rather than ``Grade.judge_status``.
_WIRE_SHAPE_PACK_IDS: tuple[str, ...] = (
    "rubric_only",
    "state_plus_judge",
    "all_four_no_hash",
)

_HASH_REFUSAL_PACK_ID = "hash_and_all_four"

_DB_PROBES_PACK_ID = "state_checks_db_probes_only"

# The runner-side substrate service lives on the runner container's gRPC
# listen port (`RUNNER_EXPOSE_SUBSTRATE=true` on the runner container). The grader
# opens its substrate channel from inside its own container, so the address
# is docker service-name DNS rather than a host-published port.
_RUNNER_SUBSTRATE_INTERNAL_ADDR = "runner:50051"

_RUNNER_HOST_ADDR = "localhost:50051"
_GRADER_HOST_ADDR = "localhost:50052"

# Compose bring-up wait budget. Published images that predate the
# rag-service model bake still download all-MiniLM-L6-v2 on first start,
# so the wait stays download-sized to keep the smoke stable across all
# published tags rc-smoke targets.
_COMPOSE_WAIT_TIMEOUT_S = 300

_PARITY_PROJECT = "tf-parity-rc-smoke"


def _tag() -> str:
    """The tag under test; the module ``skipif`` guarantees it is set here."""
    tag = smoke_image_tag()
    assert tag is not None
    return tag


@pytest.fixture(scope="module")
def parity_stack(docker_daemon: None) -> Iterator[StackHandle]:
    """Bring up the standalone stack at the rc-smoke tag; tear down on exit.

    The published images must pull successfully; a pull miss fails the
    fixture rather than silently degrading, matching
    :mod:`tests.integration.deploy.test_published_images_rc_smoke`. Compose
    interpolation reads ``TOLOKAFORGE_IMAGE_TAG`` off the process env; the
    conftest helper wires it in for us via ``_compose_env``.
    """
    tag = _tag()
    if not pull_published(tag):
        pytest.fail(f"could not pull tolokasoft1/tolokaforge-*:{tag} for parity rc-smoke")
    up = compose(
        _PARITY_PROJECT,
        ["up", "-d", "--wait", "--wait-timeout", str(_COMPOSE_WAIT_TIMEOUT_S)],
        tag,
    )
    try:
        assert up.returncode == 0, (
            f"`compose up --wait` failed at tag {tag!r} (rc={up.returncode}):\n"
            f"stdout:\n{up.stdout}\nstderr:\n{up.stderr}"
        )
        yield StackHandle(mode="published", project=_PARITY_PROJECT, tag=tag)
    finally:
        compose(_PARITY_PROJECT, ["down", "-v"], tag)


def _register_pack_trial(pack: ParityPack) -> None:
    """Register the pack's trial on the running runner container.

    Builds a synthetic :class:`~tolokaforge.core.trial.TrialSpec` around the
    pack's :class:`TaskDescription` (which already carries the pack's
    ``initial_state`` and, for the custom-checks pack, base64-encoded
    ``tool_artifacts``) and calls ``RegisterTrial`` over gRPC. The runner
    initialises db-service with the pack's tables and stores the trial
    context so :class:`SubstrateService` can answer the grader's per-trial
    substrate reads.

    The synthetic ``agent_model_config`` is a placeholder — the trial is
    never executed under this fixture, only graded, so the field satisfies
    TrialSpec's non-null requirement without dialling any LLM.
    """
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
    from tolokaforge.core.trial import EnvEndpoints, TrialSpec

    trial_spec = TrialSpec(
        trial_id=pack.trial_id,
        run_id="parity-rc-smoke",
        task=pack.task_description,
        agent_model_config=ModelConfig(provider="openai", name="gpt-4o-mini"),
        judge_model_config=pack.judge_model_config,
        env_endpoints=EnvEndpoints(
            db_url="http://db-service:8000",
            runner_url=f"http://{_RUNNER_SUBSTRATE_INTERNAL_ADDR}",
        ),
    )
    with GrpcRunnerClient(runner_address=_RUNNER_HOST_ADDR) as client:
        client.connect(timeout=30)
        result = client.register_trial(
            trial_id=pack.trial_id,
            trial_spec_json=trial_spec.model_dump_json(),
        )
    assert result[
        "success"
    ], f"RegisterTrial failed for pack {pack.directory.name!r}: {result['error']!r}"


def _call_grade_over_wire(pack: ParityPack):
    """Send a Grade RPC to the grader container and return the raw response.

    Uses :class:`grader_pb2_grpc.GraderServiceStub` directly rather than the
    :class:`GrpcGraderClient` wrapper — the wrapper decodes the wire into a
    dict, and byte-parity assertions need the raw ``grader_pb2.Grade`` proto
    the harness's :func:`serialise_grade` projects onto the canonical dict.
    """
    from tolokaforge.grader import grader_pb2, grader_pb2_grpc

    judge_json = (
        pack.judge_model_config.model_dump_json() if pack.judge_model_config is not None else ""
    )
    request = grader_pb2.GradeRequest(
        trial_id=pack.trial_id,
        llm_messages_json=pack.llm_messages_json,
        termination_reason=pack.termination_reason,
        task_config_json=pack.grading_config.model_dump_json(),
        judge_model_config_json=judge_json,
        task_description_json=pack.task_description.model_dump_json(),
        runner_substrate_address=_RUNNER_SUBSTRATE_INTERNAL_ADDR,
    )
    with grpc.insecure_channel(_GRADER_HOST_ADDR) as channel:
        stub = grader_pb2_grpc.GraderServiceStub(channel)
        return stub.Grade(request)


def _baseline_components(pack: ParityPack) -> dict[str, float]:
    """The ``components`` mapping from the pack's committed baseline JSON."""
    baseline = json.loads(read_baseline(pack))
    components = baseline.get("components")
    assert isinstance(
        components, dict
    ), f"pack {pack.directory.name!r} baseline missing `components` mapping"
    return {str(k): float(v) for k, v in components.items()}


_NON_JUDGE_COMPONENT_FIELDS: tuple[str, ...] = (
    "state_checks",
    "transcript_rules",
    "trace_checks",
    "custom_checks",
)


@pytest.mark.parametrize("pack_id", _DETERMINISTIC_PACK_IDS, ids=list(_DETERMINISTIC_PACK_IDS))
def test_reference_pack_parity_deterministic_tier(
    pack_id: str,
    parity_stack: StackHandle,  # noqa: ARG001 — fixture usage; compose stack must be up
) -> None:
    """Each no-judge pack grades to byte-identical Grade under real containers.

    The grader returns the same wire ``Grade`` the canonical harness records
    in the pack's committed baseline — the shipped parity guarantee. A
    diff here means either a real between-legs divergence or that the
    baseline needs regenerating; the parity gate does not tell one from
    the other, but its ``AssertionError`` message names the pack.

    ``state_checks_db_probes_only`` skips: its ``db_probes.dsn`` points at
    an ``app-db`` postgres absent from the standalone compose stack, so
    the runner's real ``_fetch_probe_rows`` cannot reach the pack-declared
    rows. Canonical parity via the harness's monkeypatched
    ``_fetch_probe_rows`` covers the pack.
    """
    if pack_id == _DB_PROBES_PACK_ID:
        pytest.skip(
            "db_probes DSN points at an app-db postgres absent from the standalone "
            "compose stack; parity is covered at canonical tier via monkeypatched "
            "_fetch_probe_rows"
        )
    pack = load_parity_pack(_BASELINES_ROOT / pack_id)
    _register_pack_trial(pack)
    response = _call_grade_over_wire(pack)

    assert response.success, (
        f"grader refused a deterministic-tier pack {pack_id!r} — "
        f"success=false is a regression this gate catches: {response.error!r}"
    )
    assert (
        not response.no_verdict
    ), f"grader reported no_verdict on deterministic-tier pack {pack_id!r}"
    serialised = serialise_grade(response.grade)
    baseline = read_baseline(pack)
    assert serialised == baseline, (
        f"grader_rpc over real containers diverged from the committed baseline "
        f"for deterministic-tier pack {pack_id!r}. Either the parity contract "
        f"broke or the canonical baseline needs regenerating "
        f"(--refresh-baselines at canonical tier)."
    )


@pytest.mark.parametrize("pack_id", _WIRE_SHAPE_PACK_IDS, ids=list(_WIRE_SHAPE_PACK_IDS))
def test_reference_pack_parity_wire_shape_tier(
    pack_id: str,
    parity_stack: StackHandle,  # noqa: ARG001 — fixture usage; compose stack must be up
) -> None:
    """Each judge pack keylessly errors the judge and preserves non-judge components.

    Runs against a stack with no LLM keys in the container env: the judge
    dispatch construction succeeds (LLMClient accepts an empty key chain),
    the loop's first ``.generate()`` call raises an auth error inside
    litellm, and :class:`LLMJudge.run` catches it into a fail-loud
    ``JudgeStatus.ERRORED`` result. Byte-parity on ``judge_status`` and
    ``JUDGE ERRORED`` in ``reasons`` proves the wire shape; byte-parity on
    non-judge components against the baseline proves those seams still
    produced the same output the canonical lane records.

    Refuses ``success=false``: on a judge-using pack that would mean the
    grader gave up before completing composite dispatch, which is a
    regression distinct from the intentional errored-judge outcome and one
    this gate exists to catch.
    """
    from tolokaforge.grader import grader_pb2

    pack = load_parity_pack(_BASELINES_ROOT / pack_id)
    _register_pack_trial(pack)
    response = _call_grade_over_wire(pack)

    assert response.success, (
        f"wire-shape tier pack {pack_id!r} came back success=false — the grader "
        f"gave up before completing composite dispatch, which is a regression "
        f"distinct from the intentional errored-judge outcome. error={response.error!r}"
    )
    assert (
        not response.no_verdict
    ), f"wire-shape tier pack {pack_id!r} reported no_verdict on a judge-using trial"
    grade = response.grade
    assert grade.judge_status == grader_pb2.JUDGE_STATUS_ERRORED, (
        f"wire-shape tier pack {pack_id!r} did not report a keyless-judge ERRORED "
        f"outcome (judge_status={grade.judge_status}); the runner container may "
        f"be carrying an unexpected LLM key or the judge dispatch was skipped"
    )
    assert "JUDGE ERRORED" in grade.reasons, (
        f"wire-shape tier pack {pack_id!r} judge_status is ERRORED but reasons "
        f"lack the 'JUDGE ERRORED' segment: {grade.reasons!r}"
    )

    baseline_components = _baseline_components(pack)
    serialised = serialise_grade(grade)
    wire_components = json.loads(serialised).get("components") or {}
    for field in _NON_JUDGE_COMPONENT_FIELDS:
        if field not in baseline_components:
            continue
        assert wire_components.get(field) == baseline_components[field], (
            f"wire-shape tier pack {pack_id!r} non-judge component "
            f"{field!r} diverged from the committed baseline: "
            f"wire={wire_components.get(field)!r} baseline={baseline_components[field]!r}"
        )


def test_hash_refusal_end_to_end(
    parity_stack: StackHandle,  # noqa: ARG001 — fixture usage; compose stack must be up
) -> None:
    """The ``hash_and_all_four`` pack surfaces the documented grader refusal.

    Refusal precedes any judge dial, so no LLM key is needed: the grader's
    :class:`GraderCompositeDispatch` refuses ``state_checks.hash_enabled``
    up front and the wire returns ``GradeResponse(success=false)`` whose
    ``error`` carries the ADR-0039 refusal fragment. This gate locks that
    contract over the real gRPC wire.
    """
    pack = load_parity_pack(_BASELINES_ROOT / _HASH_REFUSAL_PACK_ID)
    assert (
        pack.refusal_mode
    ), f"{_HASH_REFUSAL_PACK_ID!r} must declare refusal_mode: true — parity.yaml drift"
    assert (
        pack.expected_error_fragment
    ), f"{_HASH_REFUSAL_PACK_ID!r} must declare expected_error_fragment — parity.yaml drift"
    _register_pack_trial(pack)
    response = _call_grade_over_wire(pack)

    assert not response.success, (
        f"grader accepted a hash-enabled pack — the ADR-0039 refusal branch "
        f"is bypassed. grade={response.grade!r}"
    )
    assert pack.expected_error_fragment in (response.error or ""), (
        f"grader refused hash-enabled pack {_HASH_REFUSAL_PACK_ID!r} but the "
        f"error message does not carry the declared fragment "
        f"{pack.expected_error_fragment!r}: {response.error!r}"
    )
