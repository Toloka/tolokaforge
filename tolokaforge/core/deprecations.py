"""Schema-shape deprecation aliases for Project-layer config models.

Every alias that renames or relocates a *YAML key* lives here as a named
coercer invoked from the owning model's ``mode="before"`` validator (or,
for path-shaped inputs, from the loader). Keeping them in one module means
the accept-and-warn surface is auditable in a single place and the models
carry only a one-line call.

Deprecation message quality bar. Every warning emitted here follows a
uniform actionable shape: *What* legacy shape triggered the warning,
*Where* it was authored (file basename, when known — threaded via
:data:`_source_context` set by the loader), *Why* it is deprecated,
*How* to migrate to the canonical form, and *When* the alias will be
removed (a ``(tracked in #NNN)`` suffix pointing at the follow-up issue
that carries the retirement schedule). See :func:`warn_deprecated`.

Scope boundary: the ``RunConfig`` dual-home lifts (``workers``,
``queue_backend``, ``stuck_heuristics``, …) are *not* here. Those are
field-level orchestrator→compute/storage migrations that resolve values
across two live homes, not schema-shape renames; they stay on
``RunConfig`` where the effective-value accessors read them.
"""

from __future__ import annotations

import difflib
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_NETWORK_POLICY_KEY = "network_policy"
_SECURITY_CONTEXT_RENAMES = {"user": "run_as_user", "group": "run_as_group"}

# Follow-up issue that tracks the retirement schedule for every warn-only
# alias introduced in milestone 9. Every deprecation message here appends a
# `(tracked in #<n>)` suffix pointing here so users can subscribe to know
# when the alias goes away.
POST_M9_STRICT_FLIP_ISSUE = "533"

# Source-path threading. The loader sets this via :func:`source_context`
# before calling into a coercer or ``construct_config``; the value is read
# by :func:`warn_deprecated` to prefix every warning with the offending
# file's basename. Falls back gracefully when no loader is in the stack
# (e.g. direct-Python model construction) — the warning just omits the
# ``in <file>`` clause.
_source_context: ContextVar[Path | None] = ContextVar("_deprecation_source", default=None)


@contextmanager
def source_context(source: Path | None) -> Iterator[None]:
    """Set the source file that any deprecation warning emitted inside
    this ``with`` block should name.

    Loader-side call sites wrap their ``construct_config(...)`` /
    ``canonicalize_actor_config(...)`` invocation with this context. Direct
    Python construction of a model doesn't set the context; the warnings
    then omit the ``in <file>`` clause.
    """
    token = _source_context.set(source)
    try:
        yield
    finally:
        _source_context.reset(token)


def _current_source_hint() -> str:
    """Return an ``" in <basename>"`` clause when the loader threaded a
    source path, else ``""``. Basename only (never absolute paths) so
    canonical error snapshots stay machine-independent.
    """
    source = _source_context.get()
    return f" in {source.name}" if source is not None else ""


def warn_deprecated(
    *,
    legacy: str,
    canonical: str,
    detail: str = "",
    follow_up_issue: str | None = POST_M9_STRICT_FLIP_ISSUE,
    stacklevel: int = 3,
) -> None:
    """Emit a uniform ``DeprecationWarning`` for a legacy→canonical rename.

    Message shape: ``"{legacy} in <file> is deprecated; use {canonical} instead.
    {detail} (tracked in #<n>)"``. The ``in <file>`` clause is added when a
    loader is in the stack (see :func:`source_context`); ``detail`` and the
    tracker suffix are added only when set.

    ``stacklevel`` defaults to ``3`` — accurate when this helper is called
    directly from a loader (three frames: user code → loader → coercer →
    ``warn_deprecated``). When invoked from a Pydantic ``mode="before"``
    validator (e.g. via ``coerce_task_packs_alias`` on ``EvaluationConfig``)
    the real stack is deeper because Pydantic inserts wrapper frames; the
    warning surfaces at the model class rather than user code. Callers that
    know their exact depth (e.g. a direct raw-``warnings.warn`` inline
    branch converted to route through this helper) can pass an explicit
    ``stacklevel`` to target user code precisely. Message content is what
    tests assert on, so the frame target is a nice-to-have, not a
    correctness property.

    ``follow_up_issue`` defaults to :data:`POST_M9_STRICT_FLIP_ISSUE` (the
    umbrella tracking every warn-only path M9 introduced). Pass a different
    string to point at a per-alias follow-up, or ``None`` to omit the
    tracker suffix entirely (e.g. for aliases with no scheduled retirement).
    """
    source_hint = _current_source_hint()
    message = f"{legacy}{source_hint} is deprecated; use {canonical} instead."
    if detail:
        message = f"{message} {detail}"
    if follow_up_issue:
        message = f"{message} (tracked in #{follow_up_issue})"
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel)


def suggest_closest_field(model: type[BaseModel], key: str) -> str:
    """The did-you-mean clause for *key* against the fields *model* declares.

    Read by both surfaces that answer an author's misspelled config key — this
    module's warn-and-drop through :func:`tolokaforge.core.project_loader.construct_config`
    and the grading gate's refusal in
    :func:`tolokaforge.core.grading.unknown_keys.refuse_unknown_grading_keys` — so
    the same typo does not send two authors reading two different sentences. They
    differ in severity and in what they append, not in the suggestion.

    Returned with a leading and a trailing space so a caller composes it into its
    own sentence: the clause carries the suggestion, not the severity.
    """
    suggestion = difflib.get_close_matches(key, list(model.model_fields), n=1)
    if not suggestion:
        return (
            f" — no close match on {model.__name__}. Remove the key or "
            f"check the schema for the correct name. "
        )
    return (
        f" — did you mean '{suggestion[0]}'? "
        f"Rename `{key}` to `{suggestion[0]}` (or remove it if unused). "
    )


def coerce_task_packs_alias(values: Any) -> Any:
    """Accept ``evaluation.task_packs`` as an alias for ``evaluation.projects``.

    Emits a ``DeprecationWarning`` when the legacy key appears with a
    non-empty value and no explicit ``projects`` key. When both keys carry
    values the loader keeps ``projects`` and drops ``task_packs`` (with a
    warning naming the collision).
    """
    if not isinstance(values, dict):
        return values
    legacy = values.get("task_packs")
    canonical = values.get("projects")
    if not legacy:
        return values
    if canonical:
        warn_deprecated(
            legacy="evaluation.task_packs (both keys set)",
            canonical="evaluation.projects",
            detail=(
                "evaluation.task_packs and evaluation.projects are both set; "
                "projects wins. Drop task_packs from the run config."
            ),
            stacklevel=2,
        )
        values["task_packs"] = []
        return values
    warn_deprecated(
        legacy="evaluation.task_packs",
        canonical="evaluation.projects",
        detail=(
            "Rename the key: `evaluation.task_packs: [...]` becomes "
            "`evaluation.projects: [...]` — value shape is unchanged."
        ),
    )
    values["projects"] = list(legacy)
    values["task_packs"] = []
    return values


def drop_retired_max_idle_turns(data: Any) -> Any:
    """Drop ``stuck_heuristics.max_idle_turns`` from a block that declares it,
    and warn.

    Both stuck-heuristics models ignore unknown keys, so the field's removal
    would otherwise drop an author's declaration without a word. Strict
    rejection deferred to a future release (tracked in #533).
    """
    if not isinstance(data, dict) or "max_idle_turns" not in data:
        return data
    data.pop("max_idle_turns")
    warn_deprecated(
        legacy="stuck_heuristics.max_idle_turns",
        canonical="transcript_rules in the task's grading.yaml",
        detail=(
            "The idle-turn heuristic this key configured is deleted: its window "
            "counted messages while its threshold counted assistant turns, so it "
            "could not fire at any threshold above 1 (ADR-0033). Whether an agent "
            "must act is a per-task question — declare "
            "`transcript_rules.tool_expectations.required_tools` or "
            "`transcript_rules.min_assistant_turns` in the task's grading.yaml."
        ),
    )
    return data


def coerce_flat_stack_fields(data: Any) -> Any:
    """Accept flat ``compose_file`` / ``runner_service`` at an
    :class:`~tolokaforge.runner.models.EnvironmentPatch` top level and
    normalise them under ``stack``. Emits a ``DeprecationWarning`` when the
    flat shape is used; raises when a key is declared both flat and under
    ``stack``.
    """
    if not isinstance(data, dict):
        return data
    legacy_keys = {k for k in ("compose_file", "runner_service") if k in data}
    if not legacy_keys:
        return data
    stack = dict(data.get("stack") or {})
    for key in sorted(legacy_keys):
        if key in stack:
            raise ValueError(
                f"EnvironmentPatch: both flat {key!r} and stack.{key} declared"
                f"{_current_source_hint()}; the flat form is legacy — declare "
                "it only under stack."
            )
        stack[key] = data.pop(key)
    data["stack"] = stack
    warn_deprecated(
        legacy="EnvironmentPatch: flat compose_file / runner_service at the top level",
        canonical="under 'stack:'",
        detail=(
            "Move the field(s) under a `stack:` sub-object — e.g. "
            "`compose_file: env.yaml` becomes `stack: {compose_file: env.yaml}`."
        ),
    )
    return data


def coerce_network_policy_case(data: Any) -> Any:
    """Accept uppercase ``network_policy`` enum names (``NO_INTERNET`` …),
    lowercase them to the canonical enum values, and warn. Canonical
    lowercase values pass through untouched.
    """
    if not isinstance(data, dict):
        return data
    value = data.get(_NETWORK_POLICY_KEY)
    if not isinstance(value, str):
        return data
    lowered = value.lower()
    if lowered == value:
        return data
    data[_NETWORK_POLICY_KEY] = lowered
    warn_deprecated(
        legacy=f"network_policy: {value}",
        canonical=f"network_policy: {lowered}",
        detail=(
            "Network policy enum values are lowercase — lowercase the value "
            f"in place: `network_policy: {value}` becomes "
            f"`network_policy: {lowered}`."
        ),
    )
    return data


def coerce_security_context_aliases(data: Any) -> Any:
    """Accept ``user`` / ``group`` as aliases for
    :class:`~tolokaforge.runner.models.SecurityContext`'s ``run_as_user`` /
    ``run_as_group``. Renames and warns. A single source declaring both the
    legacy and canonical key with disagreeing values fails loud.
    """
    if not isinstance(data, dict):
        return data
    for legacy, canonical in _SECURITY_CONTEXT_RENAMES.items():
        if legacy not in data:
            continue
        legacy_value = data[legacy]
        if canonical in data and data[canonical] != legacy_value:
            raise ValueError(
                f"SecurityContext: legacy {legacy!r}={legacy_value!r} conflicts with "
                f"{canonical!r}={data[canonical]!r}{_current_source_hint()}; declare "
                f"only {canonical!r}."
            )
        data.pop(legacy)
        data[canonical] = legacy_value
        warn_deprecated(
            legacy=f"SecurityContext.{legacy}",
            canonical=f"SecurityContext.{canonical}",
            detail=(
                f"Rename the key in `security_context_defaults`: "
                f"`{legacy}: {legacy_value!r}` becomes "
                f"`{canonical}: {legacy_value!r}`."
            ),
        )
    return data


def canonicalize_actor_config(data: Any) -> Any:
    """Lift a legacy top-level ``user_simulator`` block into ``actors.user``.

    The user simulator is configured under ``actors.user``; top-level
    ``user_simulator`` is the legacy alias. Renames and warns. Run on **each
    config layer independently before the project→task merge** so that a
    canonical project layer and a legacy task layer both speak ``actors.user``
    and ``deep_merge`` composes them field-by-field (task wins) — a
    post-merge coercer would see both keys deposited by different layers and
    could not tell an override from a conflict.

    A single source declaring both top-level ``user_simulator`` and
    ``actors.user`` fails loud — that is an author error decidable only here,
    per-file, before the merge.
    """
    if not isinstance(data, dict) or "user_simulator" not in data:
        return data
    actors = data.get("actors")
    if actors is not None and not isinstance(actors, dict):
        return data
    if actors and "user" in actors:
        raise ValueError(
            f"A single config source declares both top-level 'user_simulator' "
            f"and 'actors.user'{_current_source_hint()}; 'user_simulator' is the "
            "legacy alias — declare only 'actors.user'."
        )
    actors = dict(actors or {})
    actors["user"] = data.pop("user_simulator")
    data["actors"] = actors
    warn_deprecated(
        legacy="Top-level 'user_simulator'",
        canonical="'actors.user'",
        detail=(
            "Move the block under actors.user: `user_simulator: {mode: llm, "
            "persona: X}` becomes `actors: {user: {mode: llm, persona: X}}`."
        ),
    )
    return data


def warn_legacy_run_config_dir(config_path: Path) -> None:
    """Emit a ``DeprecationWarning`` when a run config sits under
    ``run_config/`` (singular) instead of ``run_configs/`` (plural).
    Routes through :func:`warn_deprecated` for uniform message shape,
    threading the config file's basename via :func:`source_context` so the
    ``in <file>`` clause matches the rest of the warn-only surface.
    """
    with source_context(config_path):
        warn_deprecated(
            legacy="Legacy 'run_config/' (singular) directory",
            canonical="'run_configs/' (plural)",
            detail=(
                f"Rename the directory: `mv {config_path.parent} "
                f"{config_path.parent.parent / 'run_configs'}`."
            ),
            stacklevel=2,
        )
