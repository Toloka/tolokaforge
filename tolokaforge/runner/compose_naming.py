"""Per-trial docker-compose naming — one sanitiser shared by host and runner.

Docker Compose derives the project name from the context directory's basename
(lowercased), and its default container name for ``service`` in that project
is ``<project>_<service>``. Two callers need to agree on what those names are:

* the host-side materialiser (:mod:`tolokaforge.core.compose_materialisation`),
  which makes the per-trial temp dir whose basename becomes the project name;
* the runner-side compose-exec tools
  (:mod:`tolokaforge.runner.tool_factory` — ``bash_session`` /
  ``str_replace_editor``'s compose backends), which resolve the container to
  ``docker exec`` into from the trial id and the project-name prefix.

If the two disagree on how to sanitise the trial id, the runner execs into
"no such container" while the stack is happily up. This module owns the
sanitiser so both sides call the same code path.

Stdlib-only: the runner subset ships this file, and importing anything
orchestrator-only from a subset-shipped module would fail the runner-subset
partition invariant.
"""

from __future__ import annotations


def compose_trial_slug(trial_id: str) -> str:
    """Sanitised form of ``trial_id`` safe for a compose project / container name.

    ASCII alphanumerics and ``-`` / ``_`` are preserved; every other character
    (including ``:``, ``/``, ``.`` — the ``task_id:trial_index`` separator plus
    anything a task id could legally contain) collapses to ``_``. Idempotent
    on any string that is already valid.
    """
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in trial_id)


def compose_container_name(trial_id: str, service: str, project_prefix: str) -> str:
    """Container name for *service* in the per-trial compose stack.

    Matches Docker Compose's default naming scheme (``<project>_<service>``)
    against a project name of ``<project_prefix><compose_trial_slug(trial_id)>``.
    The host-side materialiser produces the same project name via the
    per-trial temp-dir basename, so the runner's ``docker exec`` targets the
    container the stack actually brought up.
    """
    return f"{project_prefix}{compose_trial_slug(trial_id)}_{service}"
