"""Runner subset partition — the machine-readable enumeration ADR-0025 asks for.

The runner Docker image installs a subset build target rather than the full
``tolokaforge`` wheel: this module names exactly which first-party files that
subset covers. The base wheel published to PyPI carries every file; the subset
is a Docker-only artifact built from these same sources.

The partition is derived from the runner container's actual runtime import
closure — the modules ``python -m tolokaforge.runner`` reaches from
:mod:`tolokaforge.runner.__main__` + :mod:`tolokaforge.runner.service`, plus
the lazily-loaded builtin-tool drivers the runner reconstructs from task
schemas at ``RegisterTrial``. Anything the closure never reaches at any point
in the runner's lifecycle stays base-wheel-only.

``core/`` mixes shared spine (models, LLM, grading substrate, protocols,
utility modules the closure needs) with orchestrator-only code (the
``Orchestrator`` class, dry-run, output writer, compose materialisation,
engine run state, backend factories, run-trial library entry). Rather than
reorganize the directory this ticket enumerates at file level: subpackages
whose contents are all shared-spine are named whole; subpackages with a mix
are named whole with the orchestrator-only files listed in
:data:`RUNNER_SUBSET_EXCLUDED_FILES`; individual files that live outside a
whole-package entry (the mixed root of ``core/`` and the top-level
``tolokaforge/__init__.py``) are named in :data:`RUNNER_SUBSET_LOOSE_FILES`.

The runner-subset hatch build target consumes these tuples verbatim: the
``only-include`` / ``exclude`` lists under ``[tool.hatch.build.targets.custom]``
in ``pyproject.toml`` mirror them, and the custom builder script at
``scripts/hatch/hatch_runner_subset_builder.py`` produces the
``tolokaforge_runner_subset-<version>-py3-none-any.whl`` artifact from that
enumeration. The canonical test
:mod:`tests.canonical.test_runner_subset_partition` locks the enumeration
against the actual runtime closure — and against the pyproject mirror — and
rejects drift in either direction.
"""

from __future__ import annotations

RUNNER_SUBSET_PACKAGES: tuple[str, ...] = (
    "tolokaforge/runner",
    "tolokaforge/secrets",
    "tolokaforge/tools",
    "tolokaforge/core/models",
    "tolokaforge/core/llm",
    "tolokaforge/core/grading",
)
"""Subpackage directories shipped in the runner subset.

Every ``.py`` under these paths is in the subset unless listed in
:data:`RUNNER_SUBSET_EXCLUDED_FILES`. Directory paths use forward slashes
so the value round-trips into a POSIX-style hatch enumeration unchanged on
any host platform.
"""

RUNNER_SUBSET_LOOSE_FILES: tuple[str, ...] = (
    # Top-level package init — reached because every ``tolokaforge.*`` import
    # traverses it. Ships a lazy ``__getattr__`` that resolves orchestrator
    # symbols (``Orchestrator``, metrics, run queue) via ``importlib`` — those
    # symbols raise ``AttributeError`` on the slim image because their target
    # modules are base-wheel-only. The runner never reads them.
    "tolokaforge/__init__.py",
    # Shared-spine files at the root of ``tolokaforge/core/``. Everything
    # else at the core root — the orchestrator, dry-run, config validator,
    # compose materialisation, engine run state, backend capabilities,
    # runtime / conductor / trial-grader protocol definitions, the
    # ``run_trial`` library entry, run queue, resume, project loader,
    # plugin registry, metrics, budgets, and the remaining utility modules
    # — is orchestrator-only.
    "tolokaforge/core/__init__.py",
    "tolokaforge/core/_runner_subset.py",
    "tolokaforge/core/deprecations.py",
    "tolokaforge/core/hash.py",
    "tolokaforge/core/logging.py",
    "tolokaforge/core/loop.py",
    "tolokaforge/core/netpolicy_constants.py",
    "tolokaforge/core/pricing.py",
    "tolokaforge/core/run_display_events.py",
    "tolokaforge/core/trial.py",
)
"""Individual files shipped in the subset that live outside a whole-package
entry — the top-level ``tolokaforge/__init__.py`` and the shared-spine
files at the root of ``tolokaforge/core/``.
"""

RUNNER_SUBSET_EXCLUDED_FILES: tuple[str, ...] = (
    "tolokaforge/core/grading/combine.py",
    "tolokaforge/core/grading/replay.py",
    "tolokaforge/core/grading/state_checks.py",
    "tolokaforge/core/grading/transcript.py",
    "tolokaforge/core/llm/fallback_client.py",
    "tolokaforge/tools/user_tools.py",
)
"""Files that live under a shared-spine subpackage but are orchestrator-only.

Four of them (``core.grading.combine``, ``core.grading.replay``,
``core.grading.state_checks``, ``core.tools.user_tools``) reach
orchestrator-only siblings (``core.evaluators``, ``core.output.artifacts``,
``core.utils.diff``, ``core.env_state``) — including any of these in the
subset would drag those orchestrator-only surfaces along with them, or
fail at import time inside the runner container. Two (``core.grading
.transcript``, ``core.llm.fallback_client``) have only shared-spine
imports but are consumed exclusively by orchestrator-side code and would
ship as dead weight. The runner container's runtime closure reaches none
of them.
"""


def is_in_runner_subset(rel_path: str) -> bool:
    """Return True if *rel_path* (a POSIX-slash repo-relative ``.py`` path) is
    part of the runner subset.

    A path is in the subset when it is (a) listed explicitly in
    :data:`RUNNER_SUBSET_LOOSE_FILES`, or (b) rooted under a directory in
    :data:`RUNNER_SUBSET_PACKAGES` and not listed in
    :data:`RUNNER_SUBSET_EXCLUDED_FILES`.
    """
    if rel_path in RUNNER_SUBSET_EXCLUDED_FILES:
        return False
    if rel_path in RUNNER_SUBSET_LOOSE_FILES:
        return True
    return any(rel_path.startswith(pkg + "/") for pkg in RUNNER_SUBSET_PACKAGES)
