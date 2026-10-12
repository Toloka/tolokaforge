"""Every task the adapter discovers must also materialise — and every shape stays covered.

Discovery once keyed on a compose file the corpus mostly does not ship, so
8.8% of the benchmark loaded and the remaining 888 tasks were not skipped with
a reason: they were never enumerated. Nothing failed, because absence from a
glob result looks exactly like an empty corpus.

Two locks keep that from recurring:

- **Discovery implies materialisation.** A task that loads but cannot be
  brought to a runnable environment is the same silent hole one stage later.
- **The fixture corpus keeps one of every shape.** The fixtures are the only
  corpus CI has; a shape that drops out of them stops being tested without
  any assertion changing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.task_parser import discover_tasks

pytestmark = pytest.mark.canonical

FIXTURE_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"


def _shape(task) -> str:
    """Which of the three corpus layouts the task uses."""
    if task.compose_file is None:
        return "synthesised"
    if task.compose_file.parent.name == "environment":
        return "env-compose"
    return "root-compose"


def test_every_discovered_task_materialises_into_a_runnable_stack(tmp_path):
    """Loading a task and being able to run it are one claim, not two.

    Each assertion here stands for a way the materialised stack was once
    writable but not bringable-up: a service on an image tag nothing builds, a
    build context naming a directory that was never staged, a named volume no
    top-level entry declares, or a ``${VAR}`` that expands to the empty string
    because nothing in our stack sets it.
    """
    import yaml
    from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

    adapter = TerminalBenchAdapter(
        {"terminal_bench_dir": str(FIXTURE_DIR), "staging_root": str(tmp_path)}
    )
    task_ids = adapter.get_task_ids()
    assert task_ids, "the fixture corpus is empty — discovery is broken, not passing"

    for task_id in task_ids:
        env = adapter._environment(task_id)
        assert env.compose_file.is_file(), f"{task_id}: no compose file was materialised"
        assert env.agent_service, f"{task_id}: no agent service resolved"

        root = env.compose_file.parent
        raw = env.compose_file.read_text()
        doc = yaml.safe_load(raw)
        services = doc.get("services") or {}

        agent = services.get(env.agent_service) or {}
        # No image registry is configured here, so the agent service's image is
        # a local tag this adapter invented. An image key alone would satisfy a
        # weaker assertion while naming a tag nothing in the stack ever builds,
        # which compose can only answer by trying to pull it.
        assert agent.get("build"), (
            f"{task_id}: the agent service declares no build, so its image "
            f"{agent.get('image')!r} is a local tag nothing produces"
        )

        declared_volumes = set(doc.get("volumes") or {})
        for name, body in services.items():
            body = body or {}
            build = body.get("build")
            context = build.get("context") if isinstance(build, dict) else build
            if isinstance(context, str):
                assert (
                    root / context
                ).is_dir(), (
                    f"{task_id}/{name}: build context {context!r} is not in the staging tree"
                )
            for mount in body.get("volumes") or []:
                if isinstance(mount, str) and ":" in mount:
                    source = mount.split(":")[0]
                    if not source.startswith((".", "/")):
                        assert (
                            source in declared_volumes
                        ), f"{task_id}/{name}: named volume {source!r} is never declared"

        unresolved = {
            match
            for match in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", raw)
            if not match.startswith("TOLOKAFORGE_")
        }
        assert not unresolved, f"{task_id}: unresolved compose variables {sorted(unresolved)}"


def test_the_fixture_corpus_covers_every_shape():
    """All three corpus layouts stay represented among the fixtures.

    These are the only tasks CI has, so a shape absent from them is a shape
    nothing exercises end-to-end. Pinned by name rather than by count: deleting
    the one fixture carrying a shape should fail here, not quietly narrow what
    the suite proves.
    """
    shapes = {task_id: _shape(task) for task_id, task in discover_tasks(FIXTURE_DIR).items()}
    assert {
        "echo-hello-single": "synthesised",
        "echo-hello-multi": "env-compose",
        "echo-hello": "root-compose",
    }.items() <= shapes.items()
    assert set(shapes.values()) == {"synthesised", "env-compose", "root-compose"}
