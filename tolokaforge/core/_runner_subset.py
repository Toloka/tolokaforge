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
    "tolokaforge/core/actors",
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
    # plugin registry, metrics, budgets, ``model_data_fingerprint``, and
    # the remaining utility modules — is orchestrator-only. ``model_data``
    # is included; its orchestrator-only compute sibling
    # ``model_data_fingerprint`` is not.
    "tolokaforge/core/__init__.py",
    "tolokaforge/core/_runner_subset.py",
    "tolokaforge/core/deprecations.py",
    "tolokaforge/core/hash.py",
    "tolokaforge/core/logging.py",
    "tolokaforge/core/loop.py",
    "tolokaforge/core/model_data.py",
    "tolokaforge/core/netpolicy_constants.py",
    "tolokaforge/core/pricing.py",
    "tolokaforge/core/run_display_events.py",
    "tolokaforge/core/tool_call_ids.py",
    "tolokaforge/core/trial.py",
)
"""Individual files shipped in the subset that live outside a whole-package
entry — the top-level ``tolokaforge/__init__.py`` and the shared-spine
files at the root of ``tolokaforge/core/``.

The subset-native CLI shim (:mod:`tolokaforge.runner._cli` — the ADR-0027
entry the subset wheel binds under ``[project.scripts]``) is *not* listed
here because it lives under :data:`RUNNER_SUBSET_PACKAGES` and is shipped
via that whole-package entry. Listed here anyway would double-include the
file at wheel-build time.
"""

RUNNER_SUBSET_DATA_FILES: tuple[str, ...] = (
    # Python version pin — the ``tolokaforge.docker.builder`` helper is
    # base-wheel only, but ``tolokaforge/_python_version.txt`` is included
    # here to keep the ``importlib.resources.files("tolokaforge")`` lookup
    # honest across both wheel variants when a caller reaches for it.
    "tolokaforge/_python_version.txt",
)
"""Non-Python files shipped in the subset that runtime code reads via
``importlib.resources`` at first use. Pricing, preset, and provider
binding tables are supplied by the :mod:`tolokaforge_models` wheel — the
subset wheel's ``Requires-Dist: tolokaforge-models`` pulls them into the
runner container at ``pip install`` time. Only the
``_python_version.txt`` build artefact ships directly here, discoverable
via ``importlib.resources.files("tolokaforge")`` for parity with the
base wheel."""

RUNNER_SUBSET_EXCLUDED_FILES: tuple[str, ...] = (
    "tolokaforge/core/actors/turn_policy.py",
    "tolokaforge/core/grading/agreement.py",
    "tolokaforge/core/grading/combine.py",
    "tolokaforge/core/grading/config_validation.py",
    "tolokaforge/core/grading/corpus_curation.py",
    "tolokaforge/core/grading/migration_declaration.py",
    "tolokaforge/core/grading/replay.py",
    "tolokaforge/core/grading/replay_layout.py",
    "tolokaforge/core/grading/rubric_migration.py",
    "tolokaforge/core/grading/state_checks.py",
    "tolokaforge/core/grading/trace_replay.py",
    "tolokaforge/core/grading/unknown_keys.py",
    "tolokaforge/core/llm/fallback_client.py",
    "tolokaforge/tools/user_tools.py",
)
"""Files that live under a shared-spine subpackage but are orchestrator-only.

``core.actors.turn_policy`` names the concrete built-in turn policies the
runner-side ``TrialRunner`` (orchestrator-owned) dispatches on
``TaskConfig.interaction_mode`` — its imports reach
``core.plugin_registry`` for :class:`TurnPolicyContext`, which is
orchestrator-only. The runner container never resolves a policy: the
orchestrator does that before handing a :class:`TrialRunner` to the
conductor, and the runner-side wire protocol carries only the resolved
per-turn artefacts.

Eight grading-side files (``core.grading.combine``,
``core.grading.corpus_curation``, ``core.grading.migration_declaration``,
``core.grading.replay``, ``core.grading.rubric_migration``,
``core.grading.state_checks``, ``core.grading.trace_replay``,
``core.tools.user_tools``) reach orchestrator-only siblings
(``core.output.artifacts``, ``core.output_writer``,
``core.utils.diff``, ``core.env_state``, ``adapters._task_loader``) —
including any of these in
the subset would drag those orchestrator-only surfaces along with them, or
fail at import time inside the runner container. ``migration_declaration``
reaches ``core.output.artifacts`` and ``core.output_writer`` indirectly, through
the ``corpus_curation`` import that resolves an entry's declared corpus against
the manifest ``tolokaforge curate`` writes. The remaining five
(``core.grading.agreement``, ``core.grading.config_validation``,
``core.grading.replay_layout``, ``core.grading.unknown_keys``,
``core.llm.fallback_client``) have only
shared-spine imports — ``replay_layout`` imports nothing outside the standard
library — but are consumed exclusively by orchestrator-side code: the pre-run
authoring gate, the rubric-to-trace-check migration and the offline replay
commands all run on the host, before or after any trial is scheduled, and they
would ship as dead weight. The runner container's runtime
closure reaches none of them.
"""


def is_in_runner_subset(rel_path: str) -> bool:
    """Return True if *rel_path* (a POSIX-slash repo-relative ``.py`` path) is
    part of the runner subset.

    A path is in the subset when it is (a) listed explicitly in
    :data:`RUNNER_SUBSET_LOOSE_FILES` or :data:`RUNNER_SUBSET_DATA_FILES`, or
    (b) rooted under a directory in :data:`RUNNER_SUBSET_PACKAGES` and not
    listed in :data:`RUNNER_SUBSET_EXCLUDED_FILES`.
    """
    if rel_path in RUNNER_SUBSET_EXCLUDED_FILES:
        return False
    if rel_path in RUNNER_SUBSET_LOOSE_FILES:
        return True
    if rel_path in RUNNER_SUBSET_DATA_FILES:
        return True
    return any(rel_path.startswith(pkg + "/") for pkg in RUNNER_SUBSET_PACKAGES)
