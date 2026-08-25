"""Canonical parity reference — ``runner_rpc`` vs ``grader_rpc`` byte parity.

Parameterised over the parity packs under
``tests/canonical/grader_parity_baselines/``. Each pack ships a committed
:file:`expected_grade.json` baseline; the test drives both legs in-process
and asserts every leg produces the baseline byte-for-byte.

Test tier: canonical. No live LLM key ever reaches the harness (the judge
provider's :class:`LLMClient` is monkeypatched to a scripted stand-in), and
no live postgres is ever dialled (the ``_fetch_probe_rows`` seam is
monkeypatched to serve pack-declared rows), so the lane stays keyless and
network-free.

Refresh workflow: ``pytest --refresh-baselines
tests/canonical/test_grader_parity_reference.py`` rewrites every pack's
baseline from the runner leg's output and skips the equality assertions.
The refresh is idempotent — a second run against the freshly-written
baseline produces no diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.utils.grader_parity_harness import (
    REFRESH_BASELINES_OPTION,
    ParityPack,
    assert_grader_rpc_refuses,
    components_diff,
    load_parity_pack,
    read_baseline,
    refresh_or_assert_baseline,
    run_via_grader_rpc,
    run_via_runner_rpc,
    serialise_grade,
    should_refresh_baselines,
    write_baseline,
)
from tolokaforge.grader import grader_pb2
from tolokaforge.runner.models import TraceChecksResult

pytestmark = pytest.mark.canonical


_BASELINES_ROOT = Path(__file__).parent / "grader_parity_baselines"

# One row per plug-in seam Phase 2 introduced. Each pack's grading.yaml
# declares exactly one non-trivial grading block — the isolation invariant
# ``test_isolation_pack_config_is_single_seam`` locks — so a divergence at
# that seam surfaces at that pack alone.
_ISOLATION_PACKS: list[tuple[str, str]] = [
    ("state_checks_jsonpath_only", "state_checks"),
    ("state_checks_db_probes_only", "state_checks"),
    ("transcript_rules_only", "transcript_rules"),
    ("trace_checks_heavy", "trace_checks"),
    ("custom_checks_only", "custom_checks"),
    ("rubric_only", "llm_judge"),
]
_ISOLATION_PACK_DIRS = [_BASELINES_ROOT / name for name, _ in _ISOLATION_PACKS]
_ISOLATION_PACK_IDS = [name for name, _ in _ISOLATION_PACKS]
_ISOLATION_PACK_EXPECTED_BLOCK = dict(_ISOLATION_PACKS)

_RUBRIC_ONLY_PACK_DIR = _BASELINES_ROOT / "rubric_only"

# Composite packs that assert per-component parity across two or more seams.
# The refusal case (``hash_and_all_four``) rides its own test — the grader leg
# raises rather than producing a Grade — so it is excluded from this list.
_COMPOSITE_PARITY_PACK_IDS: list[str] = [
    "state_plus_transcript",
    "state_plus_judge",
    "all_four_no_hash",
]
_COMPOSITE_PARITY_PACK_DIRS = [_BASELINES_ROOT / name for name in _COMPOSITE_PARITY_PACK_IDS]
_HASH_AND_ALL_FOUR_PACK_DIR = _BASELINES_ROOT / "hash_and_all_four"


@pytest.mark.parametrize("pack_dir", _ISOLATION_PACK_DIRS, ids=_ISOLATION_PACK_IDS)
def test_isolation_pack_parity(
    pack_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Each isolation pack's committed baseline matches both grading legs.

    Runs the two legs, serialises each to the canonical dict projection,
    and asserts both equal the committed baseline byte-for-byte. Under
    ``--refresh-baselines`` the runner leg's output overwrites the
    committed baseline and the assertion is skipped.
    """
    pack = load_parity_pack(pack_dir)
    runner_grade = run_via_runner_rpc(pack, monkeypatch=monkeypatch)
    grader_grade = run_via_grader_rpc(pack, monkeypatch=monkeypatch)
    runner_serialised = serialise_grade(runner_grade)
    grader_serialised = serialise_grade(grader_grade)
    refresh_or_assert_baseline(
        request,
        pack,
        runner_serialised=runner_serialised,
        grader_serialised=grader_serialised,
    )


@pytest.mark.parametrize("pack_dir", _ISOLATION_PACK_DIRS, ids=_ISOLATION_PACK_IDS)
def test_isolation_pack_config_is_single_seam(pack_dir: Path) -> None:
    """Each isolation pack's grading.yaml populates exactly one seam.

    Every ``state_checks`` / ``transcript_rules`` / ``trace_checks`` /
    ``llm_judge`` / ``custom_checks`` block is either the pack's declared
    seam (populated with a non-empty payload) or absent — no block outside
    the declared seam may carry data. The state-check subseams collapse
    into one wire slot (:class:`~tolokaforge.runner.models.RunnerStateChecksConfig`
    admits jsonpath and db_probes as siblings), so ``state_checks_jsonpath_only``
    and ``state_checks_db_probes_only`` both declare ``state_checks`` here and
    the jsonpath-vs-probes cross-check runs inside this same helper.
    """
    pack = load_parity_pack(pack_dir)
    expected_block = _ISOLATION_PACK_EXPECTED_BLOCK[pack_dir.name]
    populated = _populated_grading_blocks(pack)
    assert populated == {expected_block}, (
        f"pack {pack_dir.name!r} declares expected seam {expected_block!r} but "
        f"grading.yaml populates {sorted(populated)}"
    )
    if expected_block == "state_checks":
        state_checks = pack.grading_config.state_checks
        assert state_checks is not None
        if pack_dir.name == "state_checks_jsonpath_only":
            assert state_checks.jsonpath_checks and not state_checks.db_probes
        elif pack_dir.name == "state_checks_db_probes_only":
            assert state_checks.db_probes and not state_checks.jsonpath_checks


def test_refresh_baselines_flag_rewrites_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--refresh-baselines`` rewrites the pack's baseline and stops.

    Copies the rubric-only isolation pack into a temp directory so the
    committed baseline stays untouched, corrupts the copy's baseline to
    an empty object, then invokes ``refresh_or_assert_baseline`` against
    a :class:`pytest.Config` stub that reports the refresh flag. The
    :class:`pytest.skip.Exception` proves the equality assertions did
    not run; the rewritten file must equal the runner leg's serialisation.
    """
    scratch_pack_dir = tmp_path / "rubric_only"
    scratch_pack_dir.mkdir()
    for name in ("task.yaml", "grading.yaml", "trial.yaml", "parity.yaml"):
        (scratch_pack_dir / name).write_text(
            (_RUBRIC_ONLY_PACK_DIR / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (scratch_pack_dir / "expected_grade.json").write_text("{}\n", encoding="utf-8")

    scratch_pack = load_parity_pack(scratch_pack_dir)
    runner_grade = run_via_runner_rpc(scratch_pack, monkeypatch=monkeypatch)
    grader_grade = run_via_grader_rpc(scratch_pack, monkeypatch=monkeypatch)
    runner_serialised = serialise_grade(runner_grade)
    grader_serialised = serialise_grade(grader_grade)

    request_stub = _RequestStub(refresh_baselines=True)
    with pytest.raises(pytest.skip.Exception, match="baselines refreshed"):
        refresh_or_assert_baseline(
            request_stub,
            scratch_pack,
            runner_serialised=runner_serialised,
            grader_serialised=grader_serialised,
        )

    rewritten = (scratch_pack_dir / "expected_grade.json").read_text(encoding="utf-8")
    assert rewritten == runner_serialised + "\n"

    idempotent_request = _RequestStub(refresh_baselines=False)
    refresh_or_assert_baseline(
        idempotent_request,
        scratch_pack,
        runner_serialised=runner_serialised,
        grader_serialised=grader_serialised,
    )
    assert read_baseline(scratch_pack) == runner_serialised


def test_canonical_proto_to_dict_projection_is_stable() -> None:
    """The wire projection is deterministic and preserves scalar defaults.

    Two projections of the same message return the same string.
    Floats round-trip through the ``%.6g`` normaliser, so a
    numerically-equivalent double rendered with a different string
    representation across proto builds still lands as byte-identical.
    Default-valued scalar fields (``0.0`` component scores) stay
    distinguishable from the ``-1.0`` not-evaluated sentinel:
    ``always_print_fields_with_no_presence=True`` preserves both. The
    proto3-``optional`` ``trace_checks`` component materialises with the
    ``-1.0`` sentinel when the wire carries no presence — the two legs'
    absent-vs-sentinel encodings mean the same "not evaluated" and the
    projection collapses them onto the same canonical shape.
    """
    grade = grader_pb2.Grade(
        binary_pass=True,
        score=0.123456789,
        components=grader_pb2.GradeComponents(
            state_checks=0.0,
            transcript_rules=-1.0,
            llm_judge=1.0,
            custom_checks=-1.0,
        ),
        reasons="ok",
        judge_status=grader_pb2.JUDGE_STATUS_COMPLETED,
    )
    first = serialise_grade(grade)
    second = serialise_grade(grade)
    assert first == second

    parsed = json.loads(first)
    components = parsed["components"]
    assert components["state_checks"] == 0.0
    assert components["transcript_rules"] == -1.0
    assert components["llm_judge"] == 1.0
    assert components["custom_checks"] == -1.0
    assert components["trace_checks"] == -1.0
    assert parsed["score"] == float(f"{0.123456789:.6g}")

    presence_grade = grader_pb2.Grade(
        components=grader_pb2.GradeComponents(
            state_checks=-1.0,
            transcript_rules=-1.0,
            llm_judge=-1.0,
            custom_checks=-1.0,
            trace_checks=0.75,
        ),
    )
    presence_parsed = json.loads(serialise_grade(presence_grade))
    assert presence_parsed["components"]["trace_checks"] == 0.75


def test_assert_grader_rpc_refuses_matches_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The refusal helper accepts a matching fragment and rejects a mismatch.

    Builds a scratch pack that enables ``state_checks.hash`` — the one
    branch :class:`GraderCompositeDispatch.grade` refuses, raising
    ``GradingFailedError`` with the "cannot execute hash-based grading"
    message. Asserts:

    1. ``assert_grader_rpc_refuses`` passes when the fragment is a
       substring of the raised message.
    2. It re-raises when the fragment does not match.
    """
    pack = _build_hash_enabled_scratch_pack(tmp_path)

    assert_grader_rpc_refuses(
        pack,
        expected_error_fragment="cannot execute hash-based grading",
        monkeypatch=monkeypatch,
    )

    with pytest.raises(AssertionError):
        assert_grader_rpc_refuses(
            pack,
            expected_error_fragment="this fragment is not in the refusal message",
            monkeypatch=monkeypatch,
        )


@pytest.mark.parametrize("pack_dir", _COMPOSITE_PARITY_PACK_DIRS, ids=_COMPOSITE_PARITY_PACK_IDS)
def test_composite_pack_parity(
    pack_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Each composite pack's committed baseline matches both grading legs.

    Runs both legs and asserts byte-for-byte equality against the pack's
    committed baseline. Under ``--refresh-baselines`` the runner leg's
    output overwrites the baseline and the assertion is skipped. Refusal
    packs are covered by :func:`test_composite_pack_parity_hash_refusal`.
    """
    pack = load_parity_pack(pack_dir)
    runner_grade = run_via_runner_rpc(pack, monkeypatch=monkeypatch)
    grader_grade = run_via_grader_rpc(pack, monkeypatch=monkeypatch)
    runner_serialised = serialise_grade(runner_grade)
    grader_serialised = serialise_grade(grader_grade)
    refresh_or_assert_baseline(
        request,
        pack,
        runner_serialised=runner_serialised,
        grader_serialised=grader_serialised,
    )


def test_composite_pack_parity_hash_refusal(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """The ``hash_and_all_four`` pack captures the accepted refusal divergence.

    The runner leg grades every component (hash grading resolves to
    ``hash_match=True`` against the harness's stationary substrate; the
    other four components score alongside), and its serialised output is
    the committed baseline. The grader leg refuses on the
    ``hash_enabled`` branch — the expected-error fragment declared on
    ``parity.yaml`` is the entire grader-side contract; no grader baseline
    JSON is captured.
    """
    pack = load_parity_pack(_HASH_AND_ALL_FOUR_PACK_DIR)
    assert pack.refusal_mode, f"{pack.directory.name!r} pack must declare refusal_mode: true"
    assert (
        pack.expected_error_fragment
    ), f"{pack.directory.name!r} pack must declare expected_error_fragment"
    runner_grade = run_via_runner_rpc(pack, monkeypatch=monkeypatch)
    runner_serialised = serialise_grade(runner_grade)
    if should_refresh_baselines(request.config):
        write_baseline(pack, runner_serialised)
        pytest.skip("baselines refreshed")
    baseline = read_baseline(pack)
    assert runner_serialised == baseline, (
        "runner_rpc leg diverged from the committed baseline; "
        "run pytest with --refresh-baselines to accept the new baseline."
    )
    assert_grader_rpc_refuses(
        pack,
        expected_error_fragment=pack.expected_error_fragment,
        monkeypatch=monkeypatch,
    )


@pytest.mark.parametrize(
    "pack_dir",
    [*_COMPOSITE_PARITY_PACK_DIRS, _HASH_AND_ALL_FOUR_PACK_DIR],
    ids=[*_COMPOSITE_PARITY_PACK_IDS, "hash_and_all_four"],
)
def test_composite_pack_judge_does_not_enable_kb_passthrough(pack_dir: Path) -> None:
    """No composite pack turns on KB passthrough on its judge.

    ADR-0039 records KB passthrough as an accepted grader-vs-runner
    divergence (the grader image cannot reconstruct the runner's KB
    corpus). A composite pack that enabled it would drift the two legs
    on a component that isn't the seam under test, so the isolation
    invariant only holds while every ``LLMJudgeConfig.customization``
    declares :attr:`disable_knowledge_search` either as ``None`` (unset)
    or ``True`` (explicitly disabled). ``False`` — the one setting that
    keeps KB tools on the judge's surface — is refused here.
    """
    pack = load_parity_pack(pack_dir)
    llm_judge = pack.grading_config.llm_judge
    if llm_judge is None:
        return
    customization = llm_judge.customization
    if customization is None:
        return
    assert customization.disable_knowledge_search is not False, (
        f"pack {pack_dir.name!r} enables KB passthrough on its judge "
        f"(disable_knowledge_search=False) — composite parity packs must not "
        "exercise the ADR-0039 KB-passthrough divergence."
    )


def test_regression_sim_baseline_flip_names_the_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-component flip in a scratch baseline surfaces at that component.

    Copies ``all_four_no_hash`` to a temp directory, mutates its baseline
    so ``components.trace_checks`` reads ``0.0`` while every other slot
    matches the runner leg's output, then invokes
    :func:`refresh_or_assert_baseline`. The raised ``AssertionError`` names
    ``trace_checks`` and only ``trace_checks`` — proving the diff-naming
    machinery reports the seam an operator must inspect. The committed
    baseline is never touched.
    """
    pack = _copy_pack_to_tmp(_BASELINES_ROOT / "all_four_no_hash", tmp_path)
    runner_grade = run_via_runner_rpc(pack, monkeypatch=monkeypatch)
    grader_grade = run_via_grader_rpc(pack, monkeypatch=monkeypatch)
    runner_serialised = serialise_grade(runner_grade)
    grader_serialised = serialise_grade(grader_grade)
    _flip_component_in_baseline(pack, "trace_checks", 0.0)

    request_stub = _RequestStub(refresh_baselines=False)
    with pytest.raises(AssertionError) as excinfo:
        refresh_or_assert_baseline(
            request_stub,
            pack,
            runner_serialised=runner_serialised,
            grader_serialised=grader_serialised,
        )
    message = str(excinfo.value)
    assert "trace_checks" in message, message
    for other in ("state_checks", "transcript_rules", "llm_judge", "custom_checks"):
        assert (
            other not in message
        ), f"baseline flip on trace_checks should not name {other!r}: {message}"


def test_regression_sim_leg_divergence_names_the_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leg-scoped scoring bug on ``grade_trace_checks`` surfaces at that seam.

    Runs the runner leg with production :func:`grade_trace_checks`, then
    monkeypatches the composite module's binding to a stub that scores
    ``0.0`` and runs the grader leg. The two serialisations then diverge
    only on the ``trace_checks`` component score — proving the parity gate
    would name that seam on a real between-legs code divergence, not just
    on a baseline mutation.
    """
    pack = load_parity_pack(_BASELINES_ROOT / "all_four_no_hash")
    runner_grade = run_via_runner_rpc(pack, monkeypatch=monkeypatch)
    runner_serialised = serialise_grade(runner_grade)

    monkeypatch.setattr(
        "tolokaforge.core.grading.composite.grade_trace_checks",
        _fake_grade_trace_checks,
    )
    grader_grade = run_via_grader_rpc(pack, monkeypatch=monkeypatch)
    grader_serialised = serialise_grade(grader_grade)

    diverging = components_diff(runner_serialised, grader_serialised)
    assert diverging == ["trace_checks"], diverging


def _fake_grade_trace_checks(*_args: Any, **_kwargs: Any) -> TraceChecksResult:
    """Divergent stub for the regression-sim leg-divergence test.

    Returns an empty :class:`TraceChecksResult` — its ``score`` defaults to
    ``-1.0`` (the "not evaluated" sentinel) and ``constraints`` is empty, so
    the composite dispatcher folds a missing ``trace_checks`` slot alongside
    a production runner leg that scored the same constraint at ``1.0``.
    """
    return TraceChecksResult()


def _copy_pack_to_tmp(source_dir: Path, tmp_path: Path) -> ParityPack:
    """Copy a committed pack into ``tmp_path`` and return the loaded pack.

    Mirrors the small fixed set of files a composite pack ships. The temp
    copy carries its own writable baseline so
    ``test_regression_sim_baseline_flip_names_the_component`` can mutate
    without touching the committed one.
    """
    scratch_dir = tmp_path / source_dir.name
    scratch_dir.mkdir()
    for name in (
        "task.yaml",
        "grading.yaml",
        "trial.yaml",
        "parity.yaml",
        "expected_grade.json",
        "checks.py",
    ):
        source = source_dir / name
        if not source.exists():
            continue
        (scratch_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return load_parity_pack(scratch_dir)


def _flip_component_in_baseline(pack: ParityPack, component: str, new_value: float) -> None:
    """Rewrite one ``components[<name>]`` entry in the pack's baseline.

    Preserves the top-level JSON layout the harness writes so the flipped
    file still parses; every other field stays byte-identical to the
    committed baseline.
    """
    baseline_path = pack.directory / "expected_grade.json"
    parsed = json.loads(baseline_path.read_text(encoding="utf-8"))
    parsed.setdefault("components", {})[component] = new_value
    baseline_path.write_text(json.dumps(parsed, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _populated_grading_blocks(pack: ParityPack) -> set[str]:
    """Return the top-level grading blocks whose payload is non-empty.

    ``state_checks`` counts as populated when the block declares any
    source (jsonpath, db_probes, or hash-enabled); the four sibling
    blocks count when their model is non-None. A block declared but
    empty (e.g. ``state_checks: {}``) still populates the field on the
    parsed :class:`RunnerGradingConfig`, so the check reads through to
    the source lists — a block that scores nothing is not the isolation
    invariant that pack asserts.
    """
    grading = pack.grading_config
    populated: set[str] = set()
    if grading.state_checks and (
        grading.state_checks.jsonpath_checks
        or grading.state_checks.db_probes
        or grading.state_checks.hash_enabled
    ):
        populated.add("state_checks")
    if grading.transcript_rules is not None:
        populated.add("transcript_rules")
    if grading.trace_checks is not None:
        populated.add("trace_checks")
    if grading.llm_judge is not None:
        populated.add("llm_judge")
    if grading.custom_checks and grading.custom_checks.get("enabled"):
        populated.add("custom_checks")
    return populated


def _build_hash_enabled_scratch_pack(tmp_path: Path) -> ParityPack:
    """Materialise a temp pack whose ``grading.yaml`` enables ``state_checks.hash``.

    The grader leg refuses that branch with the "cannot execute
    hash-based grading" message; runner_rpc grading is not asserted here.
    """
    scratch_pack_dir = tmp_path / "hash_refusal_smoke"
    scratch_pack_dir.mkdir()
    for name in ("task.yaml", "trial.yaml", "parity.yaml"):
        (scratch_pack_dir / name).write_text(
            (_RUBRIC_ONLY_PACK_DIR / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (scratch_pack_dir / "grading.yaml").write_text(
        ("weights:\n  state_checks: 1.0\nstate_checks:\n  hash_enabled: true\n"),
        encoding="utf-8",
    )
    (scratch_pack_dir / "expected_grade.json").write_text("{}\n", encoding="utf-8")
    return load_parity_pack(scratch_pack_dir)


class _RequestStub:
    """Duck-typed :class:`pytest.FixtureRequest` — only ``.config.getoption``
    is exercised by :func:`refresh_or_assert_baseline`."""

    def __init__(self, *, refresh_baselines: bool) -> None:
        self.config = _ConfigStub(refresh_baselines=refresh_baselines)


class _ConfigStub:
    def __init__(self, *, refresh_baselines: bool) -> None:
        self._refresh_baselines = refresh_baselines

    def getoption(self, name: str) -> bool:
        assert name == REFRESH_BASELINES_OPTION, name
        return self._refresh_baselines
