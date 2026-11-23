"""Shape detection and v4 field parsing in :mod:`task_parser`."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.task_parser import (
    TaskShape,
    TerminalBenchArtifact,
    TerminalBenchCollectHook,
    detect_shape,
    discover_tasks,
)

pytestmark = pytest.mark.unit


def _write_task(
    base: Path, task_id: str, task_toml: str, *, tests_dockerfile: bool = False
) -> Path:
    task_dir = base / task_id
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (task_dir / "task.toml").write_text(task_toml)
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    if tests_dockerfile:
        (task_dir / "tests").mkdir()
        (task_dir / "tests" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    return task_dir


_V21_TOML = """
[metadata]
difficulty = "medium"
tags = ["demo"]

[agent]
timeout_sec = 1800.0

[verifier]
timeout_sec = 300.0

[environment]
cpus = 2
memory_mb = 4096
"""


def test_v21_task_is_not_v4(tmp_path: Path) -> None:
    _write_task(tmp_path, "plain", _V21_TOML)
    task = discover_tasks(tmp_path)["plain"]
    assert task.shape is TaskShape.SINGLE_CONTAINER
    assert task.is_v4 is False
    assert task.environment_mode is None
    assert task.artifacts == []
    assert task.has_tests_dockerfile is False


@pytest.mark.parametrize(
    ("environment_mode", "artifacts", "has_tests_dockerfile", "expected"),
    [
        (None, [], False, TaskShape.SINGLE_CONTAINER),
        ("separate", [], False, TaskShape.SEPARATE_VERIFIER),
        ("shared", [], False, TaskShape.SINGLE_CONTAINER),
        ("single", [], False, TaskShape.SINGLE_CONTAINER),
        (None, ["/app"], False, TaskShape.SEPARATE_VERIFIER),
        (None, [], True, TaskShape.SEPARATE_VERIFIER),
    ],
)
def test_detect_shape_markers(environment_mode, artifacts, has_tests_dockerfile, expected) -> None:
    assert (
        detect_shape(
            environment_mode=environment_mode,
            artifacts=artifacts,
            has_tests_dockerfile=has_tests_dockerfile,
        )
        is expected
    )


def test_environment_mode_marks_v4(tmp_path: Path) -> None:
    toml = _V21_TOML.replace(
        "[verifier]\ntimeout_sec = 300.0",
        '[verifier]\ntimeout_sec = 300.0\nenvironment_mode = "separate"',
    )
    _write_task(tmp_path, "sep", toml, tests_dockerfile=True)
    task = discover_tasks(tmp_path)["sep"]
    assert task.is_v4 is True
    assert task.environment_mode == "separate"


def test_tests_dockerfile_marks_v4(tmp_path: Path) -> None:
    _write_task(tmp_path, "withtests", _V21_TOML, tests_dockerfile=True)
    task = discover_tasks(tmp_path)["withtests"]
    assert task.is_v4 is True
    assert task.has_tests_dockerfile is True


def test_top_level_artifacts_parse_and_mark_v4(tmp_path: Path) -> None:
    toml = (
        'artifacts = ["/app", { source = "/srv/data", exclude = ["__pycache__", "*.pyc"] }]\n'
        + _V21_TOML
    )
    _write_task(tmp_path, "arts", toml, tests_dockerfile=True)
    task = discover_tasks(tmp_path)["arts"]
    assert task.is_v4 is True
    assert task.artifacts == [
        TerminalBenchArtifact(source="/app"),
        TerminalBenchArtifact(source="/srv/data", exclude=["__pycache__", "*.pyc"]),
    ]


def test_artifacts_under_verifier_are_not_top_level(tmp_path: Path) -> None:
    # A nested table is dropped on the wire; the parser must not treat it as the
    # re-materialisation set, so such a task stays the single-container shape.
    toml = _V21_TOML.replace(
        "[verifier]\ntimeout_sec = 300.0",
        '[verifier]\ntimeout_sec = 300.0\nartifacts = ["/app"]',
    )
    _write_task(tmp_path, "nested", toml)
    task = discover_tasks(tmp_path)["nested"]
    assert task.artifacts == []
    assert task.is_v4 is False


@pytest.mark.parametrize(
    "artifacts_line",
    [
        'artifacts = "/app"',  # not a list
        "artifacts = [123]",  # entry neither string nor table
        "artifacts = [{ exclude = [] }]",  # table missing source
        'artifacts = [{ source = "/app", exclude = "nope" }]',  # exclude not a list
    ],
)
def test_malformed_artifacts_raise(tmp_path: Path, artifacts_line: str) -> None:
    _write_task(tmp_path, "bad", artifacts_line + "\n" + _V21_TOML)
    with pytest.raises(ValueError):
        discover_tasks(tmp_path)


@pytest.mark.parametrize(
    "source",
    ["/logs", "/logs/verifier", "/logs/verifier/", "//logs", "/logs/../logs", "/"],
    ids=["root", "verifier-dir", "trailing-slash", "double-slash", "dot-dot", "filesystem-root"],
)
@pytest.mark.parametrize("spelling", ["bare", "table"], ids=["bare-string", "table"])
def test_an_artifact_naming_the_harness_log_tree_is_refused(
    tmp_path: Path, source: str, spelling: str
) -> None:
    """Materialising an artifact empties that path in the verifier first.

    So an artifact at or above ``/logs`` deletes ``/logs/verifier`` — where the
    reward is written — and streams the agent's log tree in as the verifier's
    own, reopening through the engine the separation the two log mounts keep.
    A pack declaring it is already broken, so it is refused at discovery rather
    than loaded and run.

    The comparison is on the normalised path, not the string: ``//logs`` and
    ``/logs/../logs`` name the same directory to docker and must not be a way
    past it.
    """
    entry = f'"{source}"' if spelling == "bare" else f'{{ source = "{source}" }}'
    _write_task(tmp_path, "sneaky", f"artifacts = [{entry}]\n" + _V21_TOML, tests_dockerfile=True)

    with pytest.raises(ValueError) as excinfo:
        discover_tasks(tmp_path)

    message = str(excinfo.value)
    assert "sneaky" in message
    assert source in message


@pytest.mark.parametrize(
    "entry",
    [
        '"/app/output"',
        '"/app"',
        '"/app/data/logs"',
        '"/logsmith"',
        '{ source = "/app/output" }',
        '{ source = "/tmp/pg.dump", service = "postgres" }',
    ],
)
def test_an_artifact_outside_the_harness_log_tree_still_parses(tmp_path: Path, entry: str) -> None:
    """The refusal is on the log tree, not on the substring ``logs``.

    ``/app/data/logs`` is a task's own directory and ``/logsmith`` only shares a
    prefix with ``/logs``; a guard that rejected either would break packs that
    have nothing to do with the harness tree.
    """
    _write_task(tmp_path, "fine", f"artifacts = [{entry}]\n" + _V21_TOML, tests_dockerfile=True)
    task = discover_tasks(tmp_path)["fine"]
    assert len(task.artifacts) == 1


def test_separate_verifier_without_tests_dockerfile_raises(tmp_path: Path) -> None:
    # Declares the separate-verifier shape via artifacts but ships no tests/Dockerfile
    # to build the verifier container from — must fail clearly at discovery.
    _write_task(tmp_path, "noverif", 'artifacts = ["/app"]\n' + _V21_TOML)
    with pytest.raises(ValueError):
        discover_tasks(tmp_path)


def test_non_string_environment_mode_raises(tmp_path: Path) -> None:
    toml = _V21_TOML.replace(
        "[verifier]\ntimeout_sec = 300.0",
        "[verifier]\ntimeout_sec = 300.0\nenvironment_mode = 7",
    )
    _write_task(tmp_path, "badmode", toml)
    with pytest.raises(ValueError):
        discover_tasks(tmp_path)


_DB_TOML = """
artifacts = [
  { source = "/app", exclude = ["*.pyc"] },
  { source = "/tmp/pg.dump", service = "postgres" },
]

[verifier]
environment_mode = "separate"
timeout_sec = 600.0

[[verifier.collect]]
service = "postgres"
command = "pg_dumpall > /tmp/pg.dump"
timeout_sec = 120.0

[[verifier.collect]]
command = "find /etc -name '*.conf' > /tmp/perms.txt"
"""


def test_collect_and_service_artifacts_parse(tmp_path: Path) -> None:
    _write_task(tmp_path, "db", _DB_TOML, tests_dockerfile=True)
    task = discover_tasks(tmp_path)["db"]
    assert task.is_v4 is True
    # artifact service= becomes src_service; a bare/omitted one stays None.
    assert [(a.source, a.src_service) for a in task.artifacts] == [
        ("/app", None),
        ("/tmp/pg.dump", "postgres"),
    ]
    # collect hooks: one sidecar-scoped, one with no service (runs in the agent).
    assert task.collect == [
        TerminalBenchCollectHook(
            command="pg_dumpall > /tmp/pg.dump", service="postgres", timeout_sec=120.0
        ),
        TerminalBenchCollectHook(
            command="find /etc -name '*.conf' > /tmp/perms.txt", service=None, timeout_sec=300.0
        ),
    ]


@pytest.mark.parametrize(
    "collect_block",
    [
        '[[verifier.collect]]\nservice = "postgres"\n',  # no command
        '[[verifier.collect]]\ncommand = "x"\nservice = ""\n',  # blank service
        '[[verifier.collect]]\ncommand = "x"\ntimeout_sec = 0\n',  # non-positive timeout
    ],
)
def test_malformed_collect_raises(tmp_path: Path, collect_block: str) -> None:
    toml = 'artifacts = ["/app"]\n[verifier]\nenvironment_mode = "separate"\n' + collect_block
    _write_task(tmp_path, "badc", toml, tests_dockerfile=True)
    with pytest.raises(ValueError):
        discover_tasks(tmp_path)


# ---------------------------------------------------------------------------
# [agent] user / [verifier] user
#
# Until 2026-10-30 both keys were read into the toml dict and then dropped, so
# the verifier ran as whatever the task image's USER happened to be. On the
# TBench Arena delivery — which seals its reward directory against the agent
# and declares `[verifier] user = "root"` — that meant the reward write failed
# with EACCES and 77 of 100 tasks scored zero whatever the agent did.
# ---------------------------------------------------------------------------

_USERS_TOML = """
[metadata]
difficulty = "hard"

[agent]
timeout_sec = 1800.0
user = "clerk"

[verifier]
timeout_sec = 600.0
user = "root"
"""


def test_declared_users_are_parsed(tmp_path: Path) -> None:
    _write_task(tmp_path, "with-users", _USERS_TOML)
    task = discover_tasks(tmp_path)["with-users"]

    assert task.agent_user == "clerk"
    assert task.verifier_user == "root"


def test_absent_users_stay_none(tmp_path: Path) -> None:
    """A task that declares neither keeps the image's USER for both."""
    _write_task(tmp_path, "no-users", _V21_TOML)
    task = discover_tasks(tmp_path)["no-users"]

    assert task.agent_user is None
    assert task.verifier_user is None


@pytest.mark.parametrize("bad", ["123", '""', "true"])
def test_a_user_that_is_not_a_non_empty_string_is_refused(tmp_path: Path, bad: str) -> None:
    """Refused loudly rather than dropped.

    Silently ignoring an unusable value is the behaviour that hid the original
    defect; a user that cannot be rendered into ``docker exec --user`` has to
    stop the run instead.
    """
    _write_task(
        tmp_path,
        "bad-user",
        f'[metadata]\ndifficulty = "easy"\n\n[verifier]\nuser = {bad}\n',
    )
    with pytest.raises(ValueError, match="verifier.user"):
        discover_tasks(tmp_path)


_BUILD_TIMEOUT_TOML = """
[metadata]
difficulty = "medium"

[agent]
timeout_sec = 1800.0

[environment]
cpus = 2
memory_mb = 4096
build_timeout_sec = 1200.0
"""


def test_declared_build_timeout_is_parsed(tmp_path: Path) -> None:
    """Every delivered TB4 pack declares one; unparsed, the build runs unbounded."""
    _write_task(tmp_path, "budgeted", _BUILD_TIMEOUT_TOML)
    assert discover_tasks(tmp_path)["budgeted"].build_timeout_sec == 1200.0


def test_an_integer_build_timeout_is_parsed_as_seconds(tmp_path: Path) -> None:
    """The corpus spells the same budget both ways (``1800`` and ``1800.0``)."""
    _write_task(
        tmp_path,
        "int-budget",
        '[metadata]\ndifficulty = "easy"\n\n[environment]\nbuild_timeout_sec = 1800\n',
    )
    assert discover_tasks(tmp_path)["int-budget"].build_timeout_sec == 1800.0


def test_a_task_declaring_no_build_timeout_stays_none(tmp_path: Path) -> None:
    """``None`` rather than a default: an invented budget would kill a build
    the task was willing to wait for."""
    _write_task(tmp_path, "no-budget", _V21_TOML)
    assert discover_tasks(tmp_path)["no-budget"].build_timeout_sec is None


@pytest.mark.parametrize("bad", ["0", "-1", '"900"', "true"])
def test_a_build_timeout_that_is_not_a_positive_number_is_refused(tmp_path: Path, bad: str) -> None:
    """Refused rather than dropped: a budget that cannot bound the subprocess
    would silently restore the unbounded build this field exists to end."""
    _write_task(
        tmp_path,
        "bad-budget",
        f'[metadata]\ndifficulty = "easy"\n\n[environment]\nbuild_timeout_sec = {bad}\n',
    )
    with pytest.raises(ValueError, match="environment.build_timeout_sec"):
        discover_tasks(tmp_path)


@pytest.mark.parametrize(
    ("section", "keys"),
    [
        pytest.param(
            "environment.network_mode", '[environment]\nnetwork_mode = "host"\n', id="agent"
        ),
        pytest.param("verifier.network_mode", '[verifier]\nnetwork_mode = "host"\n', id="verifier"),
        pytest.param(
            "verifier.environment.network_mode",
            '[verifier.environment]\nnetwork_mode = "host"\n',
            id="verifier-nested",
        ),
    ],
)
def test_an_unknown_network_mode_is_refused_not_dropped(
    tmp_path: Path, section: str, keys: str
) -> None:
    """A mode nobody resolves would leave the container on the network the task
    asked to be kept off."""
    _write_task(tmp_path, "odd-net", f'[metadata]\ndifficulty = "easy"\n\n{keys}')
    with pytest.raises(ValueError, match=section):
        discover_tasks(tmp_path)


def test_a_verifier_declaration_wins_over_its_nested_table(tmp_path: Path) -> None:
    """Packs split between the two spellings; the direct key is the specific one."""
    _write_task(
        tmp_path,
        "both-spellings",
        '[metadata]\ndifficulty = "easy"\n\n[verifier]\nnetwork_mode = "no-network"\n'
        '[verifier.environment]\nnetwork_mode = "default"\n',
    )
    assert discover_tasks(tmp_path)["both-spellings"].verifier_network_mode == "no-network"
