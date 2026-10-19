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
