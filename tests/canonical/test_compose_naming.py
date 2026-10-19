"""Per-trial compose-naming contract — host / runner agreement.

The host-side materialiser encodes the trial id into a per-trial temp-dir
basename (Docker Compose derives its project name from that basename). The
runner-side compose-exec wrappers resolve a container name from the same
trial id plus a project-name prefix. If the two sanitise differently, the
runner execs into "no such container" while the stack is happily up. This
test pins that agreement in one place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.compose_materialisation import (
    TOLOKAFORGE_TRIAL_SLUG_ENV,
    make_project_temp_dir,
    write_compose_env_file,
)
from tolokaforge.runner.compose_naming import (
    compose_container_name,
    compose_scope_slug,
    compose_trial_slug,
)

pytestmark = pytest.mark.canonical


# The host stamps ``container_name: <PREFIX>${TOLOKAFORGE_TRIAL_SLUG}_<service>``
# into the synthesized compose doc; compose resolves the placeholder from the
# per-trial ``.env`` value the materialiser writes. This mirrors that resolution
# so the test compares the host's resolved name against the runner's.
_PREFIX = "tbench_"


def _host_resolved_container_name(
    *, trial_slug_env_value: str, service: str, prefix: str = _PREFIX
) -> str:
    return f"{prefix}{trial_slug_env_value}_{service}"


class TestComposeTrialSlug:
    def test_alphanumerics_preserved(self) -> None:
        assert compose_trial_slug("task123") == "task123"

    def test_hyphens_and_underscores_preserved(self) -> None:
        assert compose_trial_slug("my-task_v1") == "my-task_v1"

    def test_colon_replaced_with_underscore(self) -> None:
        assert compose_trial_slug("task-1:0") == "task-1_0"

    @pytest.mark.parametrize("bad_char", [":", "/", ".", " ", "*", "$", "@", "#"])
    def test_non_id_characters_collapse_to_underscore(self, bad_char: str) -> None:
        assert compose_trial_slug(f"a{bad_char}b") == "a_b"

    def test_idempotent_on_already_valid_slug(self) -> None:
        slug = compose_trial_slug("task-1_0")
        assert compose_trial_slug(slug) == slug


class TestComposeContainerName:
    def test_pins_temp_dir_and_runner_slug_agreement(self) -> None:
        """One assertion pinning host/runner agreement: the runner-side
        exec resolver and the host-side per-trial temp dir embed the same
        slug. A future refactor that diverges the two sanitisers has to
        break this line first. (Argv-shape + host synthesis lock lives in
        `test_terminal_bench_exec_dispatch.py`.)"""
        trial_id = "task-1:0"
        temp_dir = make_project_temp_dir(trial_id)
        try:
            assert compose_trial_slug(trial_id) in temp_dir.name
        finally:
            temp_dir.rmdir()

    def test_empty_prefix_yields_trailing_slug_and_service(self) -> None:
        assert compose_container_name("t:1", "web", "") == "t_1_web"

    def test_empty_run_id_preserves_historical_name(self) -> None:
        # Backward compatibility: a caller with no run id in reach resolves the
        # pre-run-scope container name.
        assert compose_container_name("task-1:0", "main", _PREFIX) == "tbench_task-1_0_main"
        assert (
            compose_container_name("task-1:0", "main", _PREFIX, run_id="") == "tbench_task-1_0_main"
        )


class TestRunScopedContainerName:
    """The run id scopes the per-trial container name identically on both sides."""

    def test_host_stamp_equals_runner_resolve_with_run_id(self, tmp_path: Path) -> None:
        """Host-stamp == runner-resolve for the run-scoped scheme.

        The host writes ``TOLOKAFORGE_TRIAL_SLUG`` into the per-trial ``.env``;
        the runner resolves the container name from the trial id + run id. Both
        must compute the identical name or the runner execs into "no such
        container" while the stack is up.
        """
        trial_id = "task-1:0"
        run_id = "run-abc"
        service = "main"

        # Host side: read the slug value the materialiser actually writes.
        write_compose_env_file(tmp_path, trial_id=trial_id, stack_inputs={}, run_id=run_id)
        env_text = (tmp_path / ".env").read_text()
        slug_line = next(
            line
            for line in env_text.splitlines()
            if line.startswith(f"{TOLOKAFORGE_TRIAL_SLUG_ENV}=")
        )
        slug_value = slug_line.split("=", 1)[1]
        host_name = _host_resolved_container_name(trial_slug_env_value=slug_value, service=service)

        # Runner side.
        runner_name = compose_container_name(trial_id, service, _PREFIX, run_id=run_id)

        assert host_name == runner_name == "tbench_run-abc_task-1_0_main"

    def test_two_runs_of_the_same_task_differ(self) -> None:
        trial_id = "task-1:0"
        name_a = compose_container_name(trial_id, "main", _PREFIX, run_id="run-a")
        name_b = compose_container_name(trial_id, "main", _PREFIX, run_id="run-b")
        assert name_a != name_b
        assert name_a == "tbench_run-a_task-1_0_main"
        assert name_b == "tbench_run-b_task-1_0_main"

    def test_run_id_is_sanitised_like_the_trial_id(self) -> None:
        # A run id carrying separators collapses the same way the trial id does,
        # so the slug stays a valid compose name.
        assert compose_scope_slug("run:x/y", "t:1") == "run_x_y_t_1"
        assert (
            compose_container_name("t:1", "web", _PREFIX, run_id="run:x/y")
            == "tbench_run_x_y_t_1_web"
        )

    def test_slug_is_deterministic_from_inputs_alone(self) -> None:
        """The per-trial slug is a PURE function of (run_id, trial_id): same
        inputs always yield the same slug, with no process-local counter, no
        wall-clock, and no randomness. A remote/Temporal worker computing the
        slug from the trial inputs alone must land the identical stack name, so
        repeated calls — and calls separated by unrelated work — never diverge."""
        inputs = ("run-abc", "task-1:0")
        first = compose_scope_slug(*inputs)
        # Many calls, interleaved with other slug computations that must not
        # leave any residual state behind.
        for _ in range(100):
            compose_scope_slug("other-run", "other:9")
            assert compose_scope_slug(*inputs) == first
        # Identity extends to the full container name resolver.
        assert all(
            compose_container_name("task-1:0", "main", _PREFIX, run_id="run-abc")
            == "tbench_run-abc_task-1_0_main"
            for _ in range(10)
        )
