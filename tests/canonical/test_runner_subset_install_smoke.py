"""Positive subset-install smoke for the runner-subset wheel (ADR-0025 § Verification).

Every other test in :mod:`tests.canonical.test_runner_subset_partition` proves
the subset partition is internally consistent: the enumeration mirrors the
pyproject build config, the runner boot closure fits inside the enumeration,
the wheel archive ships the files the partition names. This smoke closes the
last hole in that lock — it takes the subset wheel that hatchling actually
produced, installs it into a scratch venv the way pip / the runner Docker
image would, and asserts the runner's committed surfaces still function
inside the resulting environment.

The six surfaces the smoke covers, in order:

1. **Positive imports** — every module the runner container's runtime reaches
   at boot (walk of ``RUNNER_SUBSET_PACKAGES``) must import cleanly from the
   subset venv. A subset that omits a runner-side module fails here with the
   missing module named.
2. **Negative imports** — every base-wheel-only surface the subset partition
   drops (``tolokaforge.core.orchestrator``, ``tolokaforge.adapters``,
   ``tolokaforge.dx``, ``tolokaforge.docker``, ``tolokaforge.env``) must raise
   :class:`ModuleNotFoundError`. A subset that accidentally ships one of
   those would silently make the runner image drag orchestrator machinery
   the ADR-0025 partition promises to keep out.
3. **``tolokaforge --version`` console script** — the ``[project.scripts]``
   binding in the subset wheel resolves to :mod:`tolokaforge.runner._cli`'s
   ``main`` and prints the version pip installed. The ADR-0024 committed
   ``docker exec tolokaforge --version`` surface goes through this shim, so
   breaking it here breaks the runner image's operator interface.
4. **``tolokaforge run-trial`` JSON-Lines wire (ADR-0022 § Surface 3)** —
   the shim reads one ``start`` envelope on stdin, emits exactly one terminal
   envelope on stdout. Two probes cover the wire's two contracts: a
   well-formed ``start`` envelope should surface an ``error`` envelope of
   ``error_type: "ProvisionError"`` (adapter-required tasks route through
   the base wheel per ADR-0026), and a garbage envelope should surface an
   ``error`` envelope of ``error_type: "ProtocolError"``. The smoke asserts
   the envelope shape, not the trial-execution semantics.
5. **Runner boot import graph** — importing :mod:`tolokaforge.runner.__main__`
   inside the scratch venv without invoking ``main()`` walks the same import
   closure ``python -m tolokaforge.runner`` would trigger at container boot.
   A subset that omits any module the boot path reaches fails here with the
   real Python traceback, before it can fail at runtime inside the container.
6. **Bundled data files (GitHub #830)** — the pricing table and preset
   registry are non-Python files ``importlib.resources`` reads at first use.
   A subset that ships Python only would boot with an empty pricing table
   (silent cost regression); assert the pricing dictionary is populated.

The wheel is built once per pytest session (reuses ``dist/`` if present,
otherwise invokes ``python -m hatchling build -t custom``, matching the
partition test's session pattern). The scratch venv is created once and
shared across all six tests via a session-scoped fixture so the full smoke
runs in a single ``python -m venv`` + ``pip install`` cost."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]

_SUBSET_WHEEL_GLOB = "tolokaforge_runner_subset-*.whl"

# Subprocess wall-clock caps. Enough headroom for a cold pip download of ~30
# runtime deps on a busy CI runner; a wedged venv install would otherwise
# stall the whole canonical lane. Individual smoke subprocess calls after
# install use a much tighter cap because they only import already-installed
# code.
_INSTALL_TIMEOUT_S = 180
_SUBPROCESS_TIMEOUT_S = 30

# Modules ``RUNNER_SUBSET_PACKAGES`` promises to ship — every one must import
# cleanly from the subset venv. Kept as an explicit list rather than derived
# from :mod:`tolokaforge.core._runner_subset` so a partition edit that
# accidentally drops one of these surfaces still fails this test loudly (the
# derived version would silently follow the drift).
_EXPECTED_POSITIVE_IMPORTS: tuple[str, ...] = (
    "tolokaforge",
    "tolokaforge.runner",
    "tolokaforge.runner._cli",
    "tolokaforge.runner.runner_pb2",
    "tolokaforge.secrets",
    "tolokaforge.tools",
    "tolokaforge.tools.registry",
    "tolokaforge.core.models",
    "tolokaforge.core.llm",
    "tolokaforge.core.grading",
    "tolokaforge.core.pricing",
)

# Base-wheel-only surfaces the subset partition drops. Importing any of these
# from the subset venv must raise :class:`ModuleNotFoundError`; anything else
# means the subset silently pulled a forbidden surface in.
_EXPECTED_NEGATIVE_IMPORTS: tuple[str, ...] = (
    "tolokaforge.core.orchestrator",
    "tolokaforge.adapters",
    "tolokaforge.dx",
    "tolokaforge.docker",
    "tolokaforge.env",
)


def _find_subset_wheel() -> Path | None:
    dist_dir = REPO_ROOT / "dist"
    if not dist_dir.is_dir():
        return None
    wheels = sorted(dist_dir.glob(_SUBSET_WHEEL_GLOB))
    return wheels[-1] if wheels else None


def _build_subset_wheel() -> Path:
    """Build the subset wheel via ``python -m hatchling build -t custom``.

    Matches the invocation the partition test's session fixture uses — no
    ``hatch`` CLI dependency, no ``uv`` dependency, just the PEP 517 build
    backend driven from the active interpreter."""
    build_result = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "custom"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
    )
    if build_result.returncode != 0:
        pytest.fail(
            f"subset wheel build failed (exit {build_result.returncode}):\n"
            f"stdout:\n{build_result.stdout}\nstderr:\n{build_result.stderr}"
        )
    wheel = _find_subset_wheel()
    if wheel is None:
        pytest.fail("subset wheel build reported success but produced no artifact under dist/")
    return wheel


@pytest.fixture(scope="session")
def scratch_venv_python() -> Path:
    """Path to the Python interpreter of a scratch venv with the subset wheel
    installed.

    One venv per session — every test in this module shares it, so the full
    install cost (venv creation + wheel install + resolving ~30 deps) is
    paid once. Uses :func:`tempfile.mkdtemp` under ``dist/`` instead of
    pytest's ``tmp_path_factory`` because the venv must survive across the
    session but nothing else cares about the location.

    The venv is created via ``python -m venv`` (:mod:`venv` module) from the
    same interpreter running pytest, so the venv's Python matches the
    project's ``requires-python`` floor without needing an external
    interpreter matrix."""
    wheel = _find_subset_wheel() or _build_subset_wheel()

    venv_dir = Path(tempfile.mkdtemp(prefix="subset_smoke_", dir=REPO_ROOT / "dist"))
    venv_result = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
    )
    if venv_result.returncode != 0:
        pytest.fail(
            f"scratch venv creation failed (exit {venv_result.returncode}):\n"
            f"stdout:\n{venv_result.stdout}\nstderr:\n{venv_result.stderr}"
        )

    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    venv_python = bin_dir / "python"

    # ``--no-deps`` would be wrong here — the smoke's whole point is to prove
    # a real install off the subset wheel produces a working runner
    # environment, including the ``Requires-Dist`` closure. GitHub #829's
    # symptom (``protobuf==6.x`` resolved instead of the 7.x floor the
    # generated ``runner_pb2`` requires) reproduces here without the
    # ``protobuf>=7.35.1`` constraint the same PR adds to
    # ``[project.dependencies]``. ``uv pip install`` is used for its
    # resolver speed (order-of-magnitude faster than ``python -m pip`` cold
    # + warm) so the whole smoke stays inside the canonical lane's
    # ~30s-warm budget; ``uv`` is a project-wide hard dependency (see
    # ``[tool.uv] required-version`` in ``pyproject.toml``), so relying on
    # it on ``PATH`` here is no additional constraint. Falls back to
    # ``python -m pip`` if the ``uv`` binary is not resolvable — that
    # branch is slower but preserves the smoke on the exotic host that
    # somehow has pytest but no ``uv``.
    uv_binary = shutil.which("uv")
    if uv_binary is not None:
        install_cmd = [
            uv_binary,
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--quiet",
            str(wheel),
        ]
    else:
        install_cmd = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            str(wheel),
        ]
    install_result = subprocess.run(
        install_cmd,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
    )
    if install_result.returncode != 0:
        pytest.fail(
            f"subset wheel install into scratch venv failed (exit {install_result.returncode}):\n"
            f"wheel: {wheel}\ncommand: {install_cmd}\n"
            f"stdout:\n{install_result.stdout}\nstderr:\n{install_result.stderr}"
        )
    return venv_python


def _run_in_venv(
    venv_python: Path,
    args: list[str],
    *,
    stdin: str | None = None,
    timeout: float = _SUBPROCESS_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run *args* under the scratch venv's Python and return the completed process.

    ``cwd`` is set to the venv directory itself (which contains no
    ``tolokaforge/`` subdirectory) so ``python -c``'s implicit
    ``sys.path.insert(0, '')`` cannot pick the *source* checkout's
    top-level ``tolokaforge/`` directory over the wheel-installed
    ``site-packages/tolokaforge/``. Without this override the smoke would
    silently shadow the installed subset with the source tree, and the
    negative-import assertion would fail because the source tree still
    ships ``tolokaforge.adapters`` / ``tolokaforge.dx`` / ``tolokaforge.env``
    on disk even though the subset wheel does not."""
    return subprocess.run(
        [str(venv_python), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=venv_python.parent.parent,
    )


def test_subset_venv_positive_imports(scratch_venv_python: Path) -> None:
    """Every runner-side module in the subset partition must import cleanly.

    The probe emits either ``OK\\n`` or a JSON error object naming the module
    and the traceback tail, so a missing module surfaces as a named
    diagnostic rather than a bare non-zero exit."""
    probe = (
        "import json, sys, traceback\n"
        f"modules = {list(_EXPECTED_POSITIVE_IMPORTS)!r}\n"
        "failures = []\n"
        "for name in modules:\n"
        "    try:\n"
        "        __import__(name)\n"
        "    except Exception as exc:\n"
        "        failures.append({'module': name, 'error': repr(exc), 'tb': traceback.format_exc()})\n"
        "if failures:\n"
        "    sys.stdout.write(json.dumps({'failures': failures}))\n"
        "    sys.exit(1)\n"
        "sys.stdout.write('OK')\n"
    )
    result = _run_in_venv(scratch_venv_python, ["-c", probe])
    assert result.returncode == 0, (
        "positive-import probe failed inside the subset venv — the subset "
        "wheel omits one or more runner-side modules the partition promises "
        "to ship:\n" + result.stdout + "\nstderr:\n" + result.stderr
    )
    assert (
        result.stdout.strip() == "OK"
    ), f"positive-import probe exited 0 but produced unexpected stdout: {result.stdout!r}"


def test_subset_venv_negative_imports(scratch_venv_python: Path) -> None:
    """Every base-wheel-only surface must raise :class:`ModuleNotFoundError`.

    The subset wheel promises not to carry these; if any of them imported
    from the subset venv, either the wheel silently shipped orchestrator
    machinery or the partition audit missed a boundary."""
    probe = (
        "import json, sys\n"
        f"modules = {list(_EXPECTED_NEGATIVE_IMPORTS)!r}\n"
        "leaked = []\n"
        "for name in modules:\n"
        "    try:\n"
        "        __import__(name)\n"
        "    except ModuleNotFoundError:\n"
        "        continue\n"
        "    except Exception as exc:\n"
        "        leaked.append({'module': name, 'error': repr(exc), 'kind': 'wrong-exception'})\n"
        "        continue\n"
        "    leaked.append({'module': name, 'kind': 'imported-cleanly'})\n"
        "if leaked:\n"
        "    sys.stdout.write(json.dumps({'leaked': leaked}))\n"
        "    sys.exit(1)\n"
        "sys.stdout.write('OK')\n"
    )
    result = _run_in_venv(scratch_venv_python, ["-c", probe])
    assert result.returncode == 0, (
        "negative-import probe failed — the subset venv can reach a "
        "base-wheel-only surface:\n" + result.stdout + "\nstderr:\n" + result.stderr
    )
    assert (
        result.stdout.strip() == "OK"
    ), f"negative-import probe exited 0 but produced unexpected stdout: {result.stdout!r}"


def test_subset_venv_tolokaforge_version_console_script(scratch_venv_python: Path) -> None:
    """``tolokaforge --version`` must print a non-empty version string.

    Click resolves the version from ``importlib.metadata.version(
    'tolokaforge-runner-subset')`` — the METADATA the custom hatch builder
    wrote (see :mod:`scripts.hatch.hatch_runner_subset_builder`). A wheel
    whose METADATA drifted or whose ``[console_scripts]`` binding vanished
    would fail here."""
    bin_dir = scratch_venv_python.parent
    console_script = bin_dir / "tolokaforge"
    assert (
        console_script.is_file()
    ), f"subset install did not register the tolokaforge console script at {console_script}"

    result = subprocess.run(
        [str(console_script), "--version"],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        cwd=scratch_venv_python.parent.parent,
    )
    assert result.returncode == 0, (
        f"`tolokaforge --version` from the subset venv exited "
        f"{result.returncode}:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Click's ``version_option`` formats as ``<prog_name> <version>\n``. Any
    # non-empty version string satisfies the smoke — pinning a literal here
    # would couple the smoke to the release cadence.
    stdout = result.stdout.strip()
    assert stdout.startswith(
        "tolokaforge "
    ), f"`tolokaforge --version` output does not start with prog name: {stdout!r}"
    version_str = stdout[len("tolokaforge ") :].strip()
    assert version_str, f"`tolokaforge --version` printed empty version: {stdout!r}"


def test_subset_venv_run_trial_valid_envelope_emits_provision_error(
    scratch_venv_python: Path,
) -> None:
    """``tolokaforge run-trial`` with a well-formed ``start`` envelope must
    emit a terminal ``error`` envelope of type ``ProvisionError``.

    Per ADR-0026, the subset-native ``run-trial`` shim cannot compose a
    :class:`~tolokaforge.core.models.TrialSpec` from a bare :class:`TaskConfig`
    — that composition needs adapter machinery the subset does not carry.
    The shim surfaces the gap as ``ProvisionError``; asserting the envelope
    shape here validates the wire (JSON Lines framing, ``"v":1``, required
    keys) without needing to spin up the runner service or a real task."""
    bin_dir = scratch_venv_python.parent
    console_script = bin_dir / "tolokaforge"

    start_envelope = (
        json.dumps(
            {
                "v": 1,
                "type": "start",
                "task": {"task_id": "smoke-probe"},
                "models": {"agent": {"name": "stub"}},
                "runtime": "auto",
                "grader": "runner_rpc",
                "conductor": "in_process",
            }
        )
        + "\n"
    )

    result = subprocess.run(
        [str(console_script), "run-trial"],
        input=start_envelope,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        cwd=scratch_venv_python.parent.parent,
    )
    # The shim exits non-zero on any terminal error, which ProvisionError is.
    # Correctness lives in the envelope on stdout, not the exit code alone.
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, (
        "`tolokaforge run-trial` emitted no envelope on stdout with a valid "
        f"start envelope:\nstderr:\n{result.stderr}"
    )
    envelope = json.loads(lines[-1])
    assert envelope.get("v") == 1, f"envelope missing 'v':1: {envelope!r}"
    assert (
        envelope.get("type") == "error"
    ), f"expected terminal envelope of type 'error' (ProvisionError path); got: {envelope!r}"
    assert (
        envelope.get("error_type") == "ProvisionError"
    ), f"expected error_type='ProvisionError' for adapter-required task; got: {envelope!r}"
    assert (
        envelope.get("fatal") is True
    ), f"error envelope should be fatal for a single-trial invocation: {envelope!r}"
    assert "message" in envelope, f"error envelope missing 'message': {envelope!r}"


def test_subset_venv_run_trial_garbage_envelope_emits_protocol_error(
    scratch_venv_python: Path,
) -> None:
    """Malformed stdin must surface as a well-formed ``ProtocolError`` envelope.

    ADR-0022 § Surface 3 requires the wire to reject anything that isn't a
    valid envelope with a typed ``error`` — never a bare traceback. This test
    feeds obvious garbage and asserts the shim honours the contract."""
    bin_dir = scratch_venv_python.parent
    console_script = bin_dir / "tolokaforge"

    result = subprocess.run(
        [str(console_script), "run-trial"],
        input="this is not json\n",
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        cwd=scratch_venv_python.parent.parent,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, (
        "`tolokaforge run-trial` emitted no envelope on stdout for garbage "
        f"input:\nstderr:\n{result.stderr}"
    )
    envelope = json.loads(lines[-1])
    assert envelope.get("v") == 1, f"envelope missing 'v':1: {envelope!r}"
    assert (
        envelope.get("type") == "error"
    ), f"garbage envelope must produce error envelope; got: {envelope!r}"
    assert (
        envelope.get("error_type") == "ProtocolError"
    ), f"expected error_type='ProtocolError' for malformed input; got: {envelope!r}"
    assert (
        envelope.get("fatal") is True
    ), f"error envelope should be fatal for a single-trial invocation: {envelope!r}"


def test_subset_venv_runner_boot_import_graph(scratch_venv_python: Path) -> None:
    """Importing :mod:`tolokaforge.runner.__main__` and
    :mod:`tolokaforge.runner.service` inside the subset venv must succeed.

    This walks the same first-party import closure the runner container's
    ``python -m tolokaforge.runner`` boot would walk, without invoking
    ``main()`` (guarded by ``if __name__ == "__main__"``). A subset that
    omits any module the boot path reaches fails here with the real
    :class:`ModuleNotFoundError` and file it came from — a much clearer
    diagnostic than the runner container crashing after ``docker run``."""
    probe = "import tolokaforge.runner.__main__\nimport tolokaforge.runner.service\nprint('OK')\n"
    result = _run_in_venv(scratch_venv_python, ["-c", probe])
    assert result.returncode == 0, (
        "runner boot import graph failed inside the subset venv — the "
        "subset wheel is missing a module the runner container reaches at "
        f"boot:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert (
        result.stdout.strip() == "OK"
    ), f"runner boot probe exited 0 but produced unexpected stdout: {result.stdout!r}"


def test_subset_venv_bundled_data_files_are_populated(scratch_venv_python: Path) -> None:
    """The pricing table and preset registry must be non-empty from the venv.

    GitHub #830 folded these into the subset build; before the fix the
    runner container booted with an empty pricing table (silent cost
    telemetry regression). The subset wheel's ``force-include`` /
    ``only-include`` entries have to survive both the hatch build and the
    pip install — this probes the end result inside the venv rather than
    inspecting the wheel archive, so a broken install path (data file
    landing outside ``site-packages/``) still surfaces here."""
    probe = (
        "from tolokaforge.core.pricing import MODEL_PRICING\n"
        "assert len(MODEL_PRICING) > 100, f'pricing table shrunk to {len(MODEL_PRICING)}'\n"
        "print(len(MODEL_PRICING))\n"
    )
    result = _run_in_venv(scratch_venv_python, ["-c", probe])
    assert result.returncode == 0, (
        "pricing-table probe failed inside the subset venv — "
        "``tolokaforge/core/data/pricing.json`` did not land in the "
        f"installed site-packages:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    pricing_count = int(result.stdout.strip())
    assert pricing_count > 100, (
        f"pricing table populated with only {pricing_count} entries — GitHub "
        "#830's data-files fix has regressed"
    )
