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


def compose_scope_slug(run_id: str, trial_id: str) -> str:
    """The per-trial slug that scopes a compose container name, optionally by run.

    Two concurrent runs of the same task share a ``trial_id``
    (``task_id:trial_index``), so a slug built from ``trial_id`` alone collides
    on container names and makes the two runs reuse one shared stack on a single
    host. When a ``run_id`` is supplied it is sanitised and prepended, so each
    run's per-trial containers are distinct. An empty ``run_id`` yields the bare
    trial slug — the historical name — so a caller that genuinely has no run id
    in reach stays backward-compatible.

    Both sides that must agree on the container name compute THIS function: the
    host-side materialiser writes it into the per-trial ``.env`` as
    ``TOLOKAFORGE_TRIAL_SLUG`` and the runner-side resolver feeds it through
    :func:`compose_container_name`. Keeping the one definition is what holds the
    host-stamp and the runner-resolve to the identical name.
    """
    trial = compose_trial_slug(trial_id)
    if not run_id:
        return trial
    return f"{compose_trial_slug(run_id)}_{trial}"


def compose_container_name(
    trial_id: str, service: str, project_prefix: str, run_id: str = ""
) -> str:
    """Container name for *service* in the per-trial compose stack.

    Matches Docker Compose's default naming scheme (``<project>_<service>``)
    against a project name of
    ``<project_prefix><compose_scope_slug(run_id, trial_id)>``. The host-side
    materialiser produces the same slug via the per-trial ``.env``'s
    ``TOLOKAFORGE_TRIAL_SLUG``, so the runner's ``docker exec`` targets the
    container the stack actually brought up — across concurrent runs once the
    run id is threaded. ``run_id`` defaults empty for the historical name.
    """
    return f"{project_prefix}{compose_scope_slug(run_id, trial_id)}_{service}"
