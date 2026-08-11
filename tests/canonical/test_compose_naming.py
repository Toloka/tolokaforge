"""Per-trial compose-naming contract — host / runner agreement.

The host-side materialiser encodes the trial id into a per-trial temp-dir
basename (Docker Compose derives its project name from that basename). The
runner-side compose-exec wrappers resolve a container name from the same
trial id plus a project-name prefix. If the two sanitise differently, the
runner execs into "no such container" while the stack is happily up. This
test pins that agreement in one place.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.compose_materialisation import make_project_temp_dir
from tolokaforge.runner.compose_naming import (
    compose_container_name,
    compose_trial_slug,
)

pytestmark = pytest.mark.canonical


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
    def test_pins_scheme_for_canonical_trial_id(self) -> None:
        """One assertion pinning host/runner agreement: the container name the
        runner-side exec resolver produces embeds the same slug the host-side
        per-trial temp dir does. A future refactor that diverges the two
        sanitisers has to break this line first."""
        trial_id = "task-1:0"
        assert compose_container_name(trial_id, "main", "tbench_") == "tbench_task-1_0_main"
        temp_dir = make_project_temp_dir(trial_id)
        try:
            assert compose_trial_slug(trial_id) in temp_dir.name
        finally:
            temp_dir.rmdir()

    def test_empty_prefix_yields_trailing_slug_and_service(self) -> None:
        assert compose_container_name("t:1", "web", "") == "t_1_web"
