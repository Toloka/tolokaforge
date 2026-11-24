"""End-to-end acceptance of the separate-verifier (4.0) grading path.

Builds a minimal 4.0 task's agent + verifier images, provisions a trial through
the real runtime backend, and grades it through the real
``RunnerServiceImpl`` path (``materialise_path`` + ``run_test_suite`` in the
verifier container), not a stand-in:

* the oracle writes the declared artifact and scores **1.0** — it is
  re-materialised from the agent into the verifier container and the suite there
  passes. The landing site is root-owned while the image's own ``USER`` is not,
  so emptying it and recreating its parent only succeeds because the staging
  carries the task's declared user too;
* a no-op writes nothing and scores **0.0** — the artifact is absent, so the
  verifier runs without it and the suite fails;
* the agent's container holds ``/logs/agent`` and **no** ``/logs/verifier``, and
  an agent that plants its own ``/logs/verifier/reward.txt = 1`` still scores
  **0.0** — the file it wrote is container-local and the trial is graded from
  the verifier's own reward, which the agent cannot reach;
* the reports the verifier writes beside that reward — the CTRF report and the
  reward-details JSON the task declares — are read out of the verifier
  container and quoted into the grade's ``reasons``, so a 0.0 names the
  criterion that failed instead of being auditable only by a re-run. The
  fixture's verifier image drops to an unprivileged ``USER`` and its suite
  leaves each report root-owned at ``0600``, so that read lands only because
  grading carries the task's declared user onto every exec it makes into the
  container.

Heavy (it builds Docker images and brings up compose stacks), so it carries the
``integration`` / ``docker`` / ``requires_docker`` markers plus a
daemon-available skip and runs only when those markers are selected — never in a
plain unit run.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.docker.image import ImageError
from tolokaforge.docker.stacks.core import core_stack

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.requires_docker]

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_TASK_ID = "v4_echo"
_RUN_ID = "test-tb-v4-e2e"
_PREBUILD_TIMEOUT_S = 600

_ALIASED_ENGINE_SERVICES: tuple[tuple[str, str], ...] = (
    ("runner", "tolokaforge-runner"),
    ("db-service", "tolokaforge-db-service"),
)


def is_docker_daemon_available() -> bool:
    """Docker daemon reachable and operational (ping + credential-store access)."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        docker.auth.load_config().get_all_credentials()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def engine_images_with_docker_cli() -> None:
    """Build ``tolokaforge-runner`` with the docker CLI baked in and alias both
    engine images as ``:local`` — the run-path preparation the synthesised task
    compose file references (the runner-side bash tool ``docker exec``s into the
    sibling agent/verifier containers via the mounted socket)."""
    stack = core_stack(enable_docker_cli=True)
    stack.build_and_prepare()
    for service_name, alias_repository in _ALIASED_ENGINE_SERVICES:
        image = stack.get_image(service_name)
        assert image is not None, f"engine image {service_name!r} did not build"
        try:
            image.add_alias_tag(alias_repository, "local")
        except ImageError as exc:
            pytest.fail(f"could not alias {service_name!r} as {alias_repository}:local: {exc}")


@pytest.fixture(scope="module")
def adapter(
    tmp_path_factory: pytest.TempPathFactory,
    engine_images_with_docker_cli: None,
) -> TerminalBenchAdapter:
    del engine_images_with_docker_cli  # fixture ordering only
    staging_root = tmp_path_factory.mktemp("tbench-v4-staging")
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(_FIXTURES),
            "task_ids": [_TASK_ID],
            "staging_root": str(staging_root),
        }
    )


@pytest.fixture(scope="module")
def prebuilt_environment(adapter: TerminalBenchAdapter) -> dict[str, Any]:
    """Materialise staging and run the adapter-declared compose builds once.

    A separate-verifier task declares two image builds — the agent and the
    verifier — so this also proves ``docker_stack_requirements()`` emits the
    verifier build and that both Dockerfiles build against a real daemon.
    """
    env = adapter._environment(_TASK_ID)
    task = adapter.to_task_description(_TASK_ID)
    requirements = adapter.docker_stack_requirements()
    built_services = {build.service for build in requirements.image_builds}
    assert built_services == {env.agent_service, env.verifier_service}, built_services
    for build in requirements.image_builds:
        subprocess.run(
            ["docker", "compose", "-f", str(build.compose_file), "build", build.service],
            check=True,
            timeout=_PREBUILD_TIMEOUT_S,
        )
    return {"env": env, "task": task}


@pytest.fixture(scope="module")
def connected_backend(
    prebuilt_environment: dict[str, Any],
) -> Iterator[SharedStackRuntimeBackend]:
    """A connected run-scope backend shared by both grading cases.

    ``connect`` brings the run-scope engine stack (runner + db-service) up once;
    each case provisions and tears down its own trial-scope stack on it.
    """
    task = prebuilt_environment["task"]
    backend = SharedStackRuntimeBackend(
        env_manifest=task.environment_manifest,
        run_id=_RUN_ID,
        mount_docker_socket=True,
    )
    backend.connect()
    try:
        yield backend
    finally:
        backend.close()


def _make_trial_spec(task_description: Any, trial_id: str) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id=_RUN_ID,
        task=task_description,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:8000",
            runner_url="http://placeholder:50051",
        ),
    )


@contextmanager
def _registered_trial(
    backend: SharedStackRuntimeBackend, task: Any, trial_id: str
) -> Iterator[str]:
    """One provisioned and registered trial, torn down on the way out."""
    spec = _make_trial_spec(task, trial_id)
    handle = backend.provision(spec)
    try:
        register = backend.register_trial(
            trial_id=spec.trial_id,
            trial_spec_json=spec.model_dump_json(exclude={"task": {"environment_manifest"}}),
        )
        assert register["success"] is True, register.get("error")
        yield spec.trial_id
    finally:
        backend.cleanup_trial(trial_id=spec.trial_id)
        backend.teardown(handle)


def _in_the_agent_container(
    backend: SharedStackRuntimeBackend, trial_id: str, command: str, call_id: str
) -> str:
    """*command*'s stdout, run as the agent would run it."""
    result = backend.execute_tool(
        trial_id=trial_id,
        tool_name="bash",
        arguments={"command": command},
        call_id=call_id,
    )
    assert result.success is True, result.error
    return result.output


def _assert_the_fixture_seals_its_reports() -> None:
    """The user seam is only under test while the fixture stays sealed.

    An image without the unprivileged ``USER``, a suite that leaves its reports
    world-readable, or an artifact destination handed to that user would let
    these assertions pass whichever user the exec ran as — a green test for the
    condition it exists to catch.
    """
    verifier_image = (_FIXTURES / _TASK_ID / "tests" / "Dockerfile").read_text(encoding="utf-8")
    suite = (_FIXTURES / _TASK_ID / "tests" / "test.sh").read_text(encoding="utf-8")
    # The LAST directive is the one the container runs as, so a later ``USER
    # root`` appended below the drop would undo it while the drop still reads.
    user_directives = [
        line.split(maxsplit=1)[1].strip()
        for line in verifier_image.splitlines()
        if line.startswith("USER ")
    ]
    assert user_directives[-1:] == ["verifier"], (
        "the v4 fixture's verifier image must END on a non-root USER, or every exec "
        f"grading makes lands as root whether or not it carries the task's user; "
        f"USER directives were {user_directives!r}"
    )
    sealed = next((line for line in suite.splitlines() if line.startswith("chmod 600 ")), "")
    for report in ("ctrf.json", "reward-details.json", "reward.txt"):
        assert report in sealed, (
            f"the v4 fixture's suite must seal {report} to the user it grades as, or an "
            f"unprivileged read of it succeeds and asserts nothing; the chmod line was "
            f"{sealed!r}"
        )
    assert "chown" not in verifier_image, (
        "the v4 fixture's artifact destination must stay root-owned, or the staging "
        "that empties it succeeds as the image's own user and asserts nothing"
    )


def _grade(backend: SharedStackRuntimeBackend, trial_id: str) -> dict[str, Any]:
    graded = backend.grade_trial(trial_id=trial_id, llm_messages_json=json.dumps([]))
    assert graded["success"] is True, graded.get("error")
    grade = graded["grade"]
    assert grade is not None
    return grade


def _provision_and_grade(
    backend: SharedStackRuntimeBackend,
    task: Any,
    trial_id: str,
    *,
    solve: bool,
) -> dict[str, Any]:
    """Provision one trial, optionally apply the oracle, grade it, tear it down.

    Returns the whole grade, so a caller can read its ``reasons`` as well as
    its score.
    """
    with _registered_trial(backend, task, trial_id) as registered:
        if solve:
            # Apply the oracle in the AGENT container; grading then re-materialises
            # /app/answer.txt into the verifier container before running the suite.
            _in_the_agent_container(backend, registered, "echo 42 > /app/answer.txt", "oracle")
        return _grade(backend, registered)


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
class TestTerminalBenchV4SeparateVerifier:
    """Oracle, no-op, isolation and report acceptance for a 4.0 task."""

    def test_oracle_solution_scores_one(
        self,
        connected_backend: SharedStackRuntimeBackend,
        prebuilt_environment: dict[str, Any],
    ) -> None:
        score = _provision_and_grade(
            connected_backend,
            prebuilt_environment["task"],
            f"{_TASK_ID}:oracle",
            solve=True,
        )["score"]
        assert score == 1.0, (
            f"the oracle writes the declared artifact, which is re-materialised into "
            f"the verifier container where the suite passes; expected 1.0, got {score}."
        )

    def test_noop_scores_zero(
        self,
        connected_backend: SharedStackRuntimeBackend,
        prebuilt_environment: dict[str, Any],
    ) -> None:
        score = _provision_and_grade(
            connected_backend,
            prebuilt_environment["task"],
            f"{_TASK_ID}:noop",
            solve=False,
        )["score"]
        assert score == 0.0, (
            f"a no-op leaves the declared artifact absent, so the verifier runs "
            f"without it and the suite fails; expected 0.0, got {score}."
        )

    def test_the_verifier_reports_reach_the_grade(
        self,
        connected_backend: SharedStackRuntimeBackend,
        prebuilt_environment: dict[str, Any],
    ) -> None:
        """A 0.0 has to say which criterion failed, or it cannot be audited.

        The reward file carries one number, so a trial whose per-criterion
        verdict is unreadable can only be explained by running the task again —
        and the directory holding the reports is the trial's compose staging,
        gone at teardown.

        The fixture seals those reports the way a hardened pack does: the
        verifier image drops to an unprivileged ``USER`` and the suite leaves
        each report root-owned at ``0600``. So the read lands only if grading
        reaches the container as the user the task declared — as the image's own
        user it is denied with ``Permission denied``, and ``unavailable`` is
        what the engine then writes in place of each report.
        """
        _assert_the_fixture_seals_its_reports()
        reasons = _provision_and_grade(
            connected_backend,
            prebuilt_environment["task"],
            f"{_TASK_ID}:reports",
            solve=False,
        )["reasons"]
        assert (
            "verifier report /logs/verifier/ctrf.json:" in reasons
        ), f"the declared CTRF report must be quoted into the grade; reasons were: {reasons!r}"
        assert '"name":"test_answer_is_42","status":"failed"' in reasons, (
            f"the grade must name the criterion that failed, not just the score; "
            f"reasons were: {reasons!r}"
        )
        assert "verifier report /logs/verifier/reward-details.json:" in reasons, (
            f"the declared reward-details report must be quoted into the grade too; "
            f"reasons were: {reasons!r}"
        )
        assert "unavailable" not in reasons, (
            f"every declared report must have been read, not recorded as unavailable — "
            f"the fixture seals its reports, so this is where an exec that dropped the "
            f"task's declared user shows up; reasons were: {reasons!r}"
        )

    def test_an_agent_planting_a_reward_still_scores_zero(
        self,
        connected_backend: SharedStackRuntimeBackend,
        prebuilt_environment: dict[str, Any],
    ) -> None:
        """The agent's container holds its own log directory and nothing else.

        ``/logs/agent`` is there to be written — the middleware usage tap and,
        for ``opencode``, the CLI's XDG state live under it. ``/logs/verifier``
        is not there at all, so the file the trial is scored by is not on the
        agent's filesystem to rewrite.

        Planting one anyway has to *succeed* and still be worthless: the agent
        is root in its own container, so ``mkdir -p /logs/verifier`` works and
        writes a container-local file. The verifier reads its own
        ``/logs/verifier/reward.txt``, written by its own suite on a host
        directory the agent never mounts, and this trial wrote no artifact — so
        the suite fails and the score is the no-op's 0.0, not the planted 1.
        """
        with _registered_trial(
            connected_backend, prebuilt_environment["task"], f"{_TASK_ID}:forgery"
        ) as trial_id:
            separated = _in_the_agent_container(
                connected_backend,
                trial_id,
                "test -d /logs/agent && test ! -e /logs/verifier && echo SEPARATED",
                "probe",
            )
            assert "SEPARATED" in separated, (
                "the agent container must carry /logs/agent and no /logs/verifier; "
                f"probe said: {separated!r}"
            )
            planted = _in_the_agent_container(
                connected_backend,
                trial_id,
                "mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt "
                "&& cat /logs/verifier/reward.txt",
                "plant",
            )
            assert planted.strip() == "1", (
                "planting the reward must succeed in the agent's own container — "
                f"a refusal would mean the trees are still joined; got {planted!r}"
            )

            score = _grade(connected_backend, trial_id)["score"]

        assert score == 0.0, (
            f"the planted reward is container-local; grading reads the verifier's own, "
            f"which this no-op trial fails; expected 0.0, got {score}."
        )
