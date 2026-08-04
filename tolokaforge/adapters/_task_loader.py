"""Task-yaml loading with optional shared-domain merge.

This module is the single entry point for turning a ``task.yaml`` path into a
validated :class:`TaskConfig`. It exists so that every code path that loads a
task — :class:`NativeAdapter`, the ``tlk_mcp_core`` adapter, the
``tolokaforge validate`` CLI, and ``tolokaforge adapter validate`` — applies
the same domain-layout merge.

The shared-domain layout lets a directory of related task cases reuse one
``_shared/domain.yaml`` file::

    <domain>/
        _shared/
            domain.yaml          # category, tools, system_prompt, mcp_server
            mcp_server.py
            system_prompt.md
        testcases/
            case_a/
                task.yaml        # domain: ../../_shared/domain.yaml
                grading.yaml
                initial_state.json
            case_b/
                task.yaml
                ...

When a ``task.yaml`` carries a ``domain:`` ref, :func:`load_task_yaml`:

1. Reads the referenced YAML.
2. Rewrites every relative path field in the domain dict so it is expressed
   relative to the *task.yaml* parent dir (not the *domain.yaml* parent dir).
3. Deep-merges the rewritten domain into the task; per-case keys win on
   conflict.
4. Strips the ``domain:`` key.
5. Validates the merged dict via :class:`TaskConfig`.
6. Returns ``(TaskConfig, effective_task_dir)``. The effective task dir is the
   *domain root* (``<domain>``) for shared-domain tasks so that downstream
   consumers — notably :meth:`NativeAdapter._bundle_task_artifacts` — pick up
   the ``_shared/`` siblings, not just the case dir.

A flat ``<task>/task.yaml`` without a ``domain:`` ref returns
``(TaskConfig, task_path.parent)`` — the legacy behaviour.

It is also the single producer of a task's agent tool set and the JSON schemas
behind it (:func:`resolve_agent_tool_schemas`), so the ``TaskDescription`` a run
puts on the wire and the inventory a validation gate reads cannot drift apart.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.deprecations import (
    canonicalize_actor_config,
    source_context,
    warn_deprecated,
)
from tolokaforge.core.grading.config_validation import (
    UNRESOLVED_COMBINE_REASON,
    UNRESOLVED_PROJECT_COMBINE_KEYS_REASON,
    AuthoringReport,
    CombineLayer,
    ReplayWorld,
    Skip,
    ToolInventory,
    inspect_grading_authoring,
)
from tolokaforge.core.grading.golden_replay import classify_initial_state
from tolokaforge.core.grading.unknown_keys import GradingKeyLayer, refuse_unknown_grading_keys
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    GradingCombineConfig,
    GradingFindingSeverity,
    TaskConfig,
    TaskDefaults,
)
from tolokaforge.core.project_loader import (
    construct_config,
    deep_merge,
    resolve_effective_grading_combine,
)

logger = get_logger(__name__)

# Keys that live on ``project.task_defaults`` but are not ``TaskConfig`` fields.
# They reach the engine through their own seams (``grading_defaults`` via
# ``NativeAdapter.get_grading_config``, ``continue_prompt`` via turn logic), so
# merging them into a task dict would fail its ``extra="forbid"`` validation.
_PROJECT_SCOPED_DEFAULT_KEYS = frozenset(TaskDefaults.model_fields) - frozenset(
    TaskConfig.model_fields
)

# Bound once so each signature's default is the value, not a call in the annotation.
_UNRESOLVED_COMBINE_LAYER = CombineLayer.unresolvable()
_UNRESOLVED_REPLAY_WORLD = ReplayWorld.unresolvable()


def validate_grading_yaml(
    grading_path: Path,
    *,
    inventory: ToolInventory,
    replay_world: ReplayWorld = _UNRESOLVED_REPLAY_WORLD,
    combine_layer: CombineLayer = _UNRESOLVED_COMBINE_LAYER,
    fail_on: GradingFindingSeverity = GradingFindingSeverity.ADVISORY,
) -> AuthoringReport:
    """Validate a task's ``grading.yaml``, failing loud on schema breaks.

    Run by ``tolokaforge validate`` so a malformed grading block is rejected at
    validate time with a clear migration message, not only at run time. ``combine``
    and ``transcript_rules`` are validated whenever either is declared; ``llm_judge``
    (the free-text ``rubric: str`` / ``output_schema`` / ``model_ref`` shapes, raised
    by :class:`LLMJudgeConfig`) and ``state_checks`` (``env_assertions`` /
    ``db_hash_check``, raised by :class:`StateChecksConfig`) carry removals. A key
    ``combine`` does not declare is refused here naming the closest declared field,
    the accepted set and which of the two layers wrote it — what the model's own
    ``extra="forbid"`` refuses on every other path in one line without an address.

    Validate is the earliest gate a task pack meets: the engine's own
    :class:`StateChecksConfig` is not constructed until artifacts are written, by
    which point the trial has already been paid for.

    Once the shapes hold, the block is checked against the tools the task gives its
    actors. Every finding is named in one raise rather than the first one found,
    because an author fixing a pack wants the list.

    A grading path that is not on disk returns an empty report: this gate reads a
    file, and whether a task may name none at all is a task-level question
    :func:`grading_source_under_adapter` answers off the adapter the task declares —
    the native adapter grades from the file, while another may synthesise its whole
    config without one.

    Args:
        grading_path: The task's grading file.
        inventory: The task's tool set. A caller that cannot resolve one passes
            :meth:`ToolInventory.unresolvable`, which skips every tool-aware rule
            into the returned report's ``unchecked`` and fails nothing.
        replay_world: What the task gives its golden actions to be replayed against.
            A caller that cannot resolve it leaves the default,
            :meth:`ReplayWorld.unresolvable`, which skips the rule reading it into
            ``unchecked`` wherever that rule would have run.
        combine_layer: What the task's enclosing project supplies beneath its own
            ``combine`` block. A caller that cannot resolve it leaves the default,
            :meth:`CombineLayer.unresolvable`, which skips the two weight rules and
            the refusal of an unknown key in the project's own block into
            ``unchecked`` for the same reason as an unresolvable inventory and with
            the same consequence.
        fail_on: The least severe finding class that raises.

    Returns:
        The authoring report, whose ``unchecked`` entries the caller is expected to
        surface: they never raise, and a gate that checked nothing must not read as
        a clean bill of health.

    Raises:
        ValueError / pydantic.ValidationError: If the grading block is invalid.
        RuntimeError: If the file or a block this gate constructs is not a mapping.
    """
    if not grading_path.exists():
        return AuthoringReport()

    with open(grading_path) as f:
        grading_data = yaml.safe_load(f) or {}

    if not isinstance(grading_data, dict):
        raise RuntimeError(
            f"Grading file {grading_path} is not a YAML mapping (got {type(grading_data).__name__})"
        )

    # The whole combine block, on any declared one: the aggregation an author names
    # decides how every component score folds, and a value outside the declared set
    # has no fold. Constructing the block rather than the one field also puts a
    # malformed ``weights`` / ``pass_threshold`` under the same gate.
    combine = grading_data.get("combine")
    if isinstance(combine, dict):
        refuse_unknown_grading_keys(
            GradingCombineConfig,
            combine,
            block_name="combine",
            layer=GradingKeyLayer.TASK,
            grading_path=grading_path,
        )
        GradingCombineConfig(**combine)
    elif combine is not None:
        raise RuntimeError(
            f"Grading file {grading_path}: 'combine' must be a mapping of method / "
            f"weights / pass_threshold, got {type(combine).__name__} ({combine!r}). "
            "A key indented one level too far under 'combine:' makes the block a list; "
            "a method written next to the key makes it a string. Write the method as "
            "'combine:' then 'method:' indented beneath it."
        )

    # The rubric migration lives on the canonical LLMJudgeConfig, so validate the
    # llm_judge block directly — independent of the surrounding grading schema,
    # which varies by adapter.
    # Validate when a rubric is present (the new gate) or when the relocated
    # ``model_ref`` lingers (so its loud migration error surfaces at validate
    # time rather than only at run time).
    llm_judge = grading_data.get("llm_judge")
    if isinstance(llm_judge, dict) and (llm_judge.get("rubric") or "model_ref" in llm_judge):
        from tolokaforge.runner.models import LLMJudgeConfig

        LLMJudgeConfig(**llm_judge)

    # Same shape for state_checks: construct the block on a removed key, and on any
    # declared ``hash`` block — whose composition weight is undecidable in one shape
    # and is the reason a pack must hear about it before the run rather than after.
    state_checks = grading_data.get("state_checks")
    if isinstance(state_checks, dict) and (
        "env_assertions" in state_checks
        or "db_hash_check" in state_checks
        or isinstance(state_checks.get("hash"), dict)
    ):
        from tolokaforge.core.models import StateChecksConfig

        StateChecksConfig(**state_checks)

    # The whole transcript_rules block, on any declared one: a turn window whose floor
    # sits above its ceiling admits no assistant-turn count, so the component is 0.0
    # however the agent behaves. Constructing the block rather than the two fields also
    # puts the ``min_assistant_turns`` domain and a misspelled key inside the
    # ``extra="forbid"`` ``tool_expectations`` under the same gate.
    transcript_rules = grading_data.get("transcript_rules")
    if isinstance(transcript_rules, dict):
        from tolokaforge.core.models import TranscriptRulesConfig

        TranscriptRulesConfig(**transcript_rules)
    elif transcript_rules is not None:
        raise RuntimeError(
            f"Grading file {grading_path}: 'transcript_rules' must be a mapping of rule "
            f"keys, got {type(transcript_rules).__name__} ({transcript_rules!r}). "
            "A key indented one level too far under 'transcript_rules:' makes the block a "
            "list. Write 'transcript_rules:' then each rule key indented one level beneath "
            "it."
        )

    # The whole trace_checks block, on any declared one: every rejection the matcher
    # vocabulary makes — an unmatchable field for the kind, a predicate asserting
    # nothing, two constraint kinds under one require — is a check that would
    # otherwise select nothing and read as an agent failure at grade time.
    trace_checks = grading_data.get("trace_checks")
    if isinstance(trace_checks, dict):
        from tolokaforge.core.models import TraceChecksConfig

        TraceChecksConfig(**trace_checks)
    elif trace_checks is not None:
        raise RuntimeError(
            f"Grading file {grading_path}: 'trace_checks' must be a mapping carrying a "
            f"'constraints' list, an 'alternatives' list, or both, got "
            f"{type(trace_checks).__name__} ({trace_checks!r}). A constraint written "
            "directly under 'trace_checks:' makes the block a list. Write 'trace_checks:' "
            "then 'constraints:' or 'alternatives:' indented beneath it."
        )

    # The project layer is refused before it is merged, because the merge takes two
    # bare dicts and can name neither the file nor which layer the key came from.
    # After the ``combine`` block's own validation above, so a malformed one has
    # already raised and the task-side value here is a mapping or nothing.
    effective_combine = None
    if combine_layer.known:
        refuse_unknown_grading_keys(
            GradingCombineConfig,
            combine_layer.project_combine or {},
            block_name="combine",
            layer=GradingKeyLayer.PROJECT,
            grading_path=grading_path,
        )
        effective_combine = resolve_effective_grading_combine(
            combine_layer.project_combine, combine
        )
    report = inspect_grading_authoring(
        grading_data, inventory, replay_world=replay_world, effective_combine=effective_combine
    )
    if effective_combine is None:
        report = report.with_unchecked(
            Skip("combine.weights", UNRESOLVED_COMBINE_REASON),
            Skip(
                "task_defaults.grading_defaults.combine",
                UNRESOLVED_PROJECT_COMBINE_KEYS_REASON,
            ),
        )
    fatal = report.fatal(fail_on)
    if fatal:
        written = "\n".join(f"  - {finding.where}: {finding.message}" for finding in fatal)
        raise ValueError(f"Grading file {grading_path} cannot be graded as written:\n{written}")
    return report


def load_task_yaml(
    task_path: Path,
    *,
    project_task_defaults: dict[str, Any] | None = None,
) -> tuple[TaskConfig, Path]:
    """Read ``task.yaml``, apply any ``domain:`` merge, return the validated config.

    All relative-path fields on the returned :class:`TaskConfig` are expressed
    relative to ``effective_task_dir``. For the flat layout that is just the
    task.yaml parent (so no rewriting is needed); for the shared-domain
    layout it is the domain root, so paths from both the domain dict and the
    case dict are normalised to that frame.

    Args:
        task_path: Absolute or relative path to ``task.yaml``.
        project_task_defaults: Optional ``project.task_defaults`` dict from
            the enclosing project. Only its ``TaskConfig``-shaped keys are
            layered into the task dict — above the adapter's Domain merge and
            below the task's own fields. Project-scoped-only keys
            (``grading_defaults``, ``continue_prompt``) are excluded here and
            reach the engine through their own seams. Precedence, low to high:
            adapter Domain bundle → project defaults → task.yaml. Task fields
            win on conflict; project defaults win over the Domain bundle where
            they overlap and the task doesn't set the field.

    Returns:
        ``(task_config, effective_task_dir)``. ``effective_task_dir`` is the
        domain root for shared-domain tasks, or ``task_path.parent`` for the
        flat layout.

    Raises:
        FileNotFoundError: If ``task_path`` does not exist.
        RuntimeError: If the ``domain:`` ref cannot be resolved or read, or if
            the YAML at either path is not a top-level mapping.
        pydantic.ValidationError: If the merged dict fails :class:`TaskConfig`
            validation (re-raised by Pydantic).
    """
    task_path = Path(task_path)
    task_root = _detect_task_root(task_path)

    with open(task_path) as f:
        task_data = yaml.safe_load(f)

    if not isinstance(task_data, dict):
        raise RuntimeError(
            f"Task file {task_path} is not a YAML mapping (got {type(task_data).__name__})"
        )

    # Rewrite case-side paths into the task_root frame *before* the domain
    # merge so we never double-rewrite domain-supplied paths. No-op for the
    # flat layout (task_root == task.yaml parent) so legacy callers see no
    # change. The ``domain`` ref itself is not in ``_PATH_FIELD_REWRITERS``
    # so this step leaves it untouched for ``_load_domain_dict`` to consume.
    if task_root != task_path.parent:
        _rewrite_task_paths(task_data, task_path.parent, task_root)

    # Load the adapter's Domain bundle (if any) but don't merge yet — the
    # merge order matters: `domain` sits below `project_task_defaults`,
    # which sits below `task.yaml`. Later layers win on conflict.
    domain_data = _load_domain_dict(task_path, task_data, task_root)
    task_data.pop("domain", None)

    # Lift each layer's legacy ``user_simulator`` into ``actors.user`` before
    # the merge, so a legacy layer and a canonical layer both speak
    # ``actors.user`` and ``deep_merge`` composes them field-by-field. Run
    # per-layer: a post-merge coercer sees both keys deposited by different
    # layers and could not tell a cross-layer override from a same-source
    # conflict. The task-side ``source_context`` names task.yaml so any
    # emitted ``DeprecationWarning`` carries the right basename; domain-
    # side coercion runs inside ``_load_domain_dict`` where the domain
    # file path is in scope; project-side coercion already ran in
    # ``load_project_config`` (this second call is idempotent).
    with source_context(task_path):
        canonicalize_actor_config(task_data)
    # domain_data already canonicalised inside _load_domain_dict (with the
    # domain file path in source_context); project_task_defaults already
    # canonicalised inside load_project_config. Both calls here were
    # redundant duplicates that could re-fire warnings without source
    # context — removed.
    _ = project_task_defaults  # no-op: canonicalisation happens upstream

    # Build the precedence chain from lowest to highest. ``deep_merge``
    # is delta-wins, so the second argument overrides the first on conflict.
    base = domain_data
    if project_task_defaults:
        task_shaped_defaults = {
            key: value
            for key, value in project_task_defaults.items()
            if key not in _PROJECT_SCOPED_DEFAULT_KEYS
        }
        base = deep_merge(base, task_shaped_defaults)
    task_data = deep_merge(base, task_data)

    # Auto-pick a sibling ``grading.yaml`` when no layer set ``grading``. An
    # explicit ``grading:`` from any layer always wins. The absolute path is
    # layout-independent and survives ``task_dir / task.grading`` joins
    # downstream unchanged, so it needs no ``_PATH_FIELD_REWRITERS`` entry.
    if not task_data.get("grading"):
        sibling_grading = task_path.parent / "grading.yaml"
        if sibling_grading.exists():
            task_data["grading"] = str(sibling_grading.resolve())

    # Resolve environment_manifest.compose_file to an absolute path
    # against the task root so ``EnvironmentManifest``'s file-existence
    # validator can locate the file regardless of the CWD at load time.
    # No-op if the manifest is absent or the path is already absolute.
    _resolve_environment_manifest_paths(task_data, task_root, task_path)

    task = construct_config(TaskConfig, task_data, source=task_path)
    task._source_dir = task_root
    return task, task_root


def load_task(
    path: str | Path,
    *,
    project_task_defaults: dict[str, Any] | None = None,
) -> TaskConfig:
    """Load a ``task.yaml`` into a validated :class:`TaskConfig`.

    Thin wrapper over :func:`load_task_yaml` that returns the config alone. The
    effective task dir is not dropped — it rides on :attr:`TaskConfig.source_dir`
    (the domain root for shared-domain tasks, the task.yaml parent otherwise), so
    the loaded task resolves its own file assets without a second return value.

    Args:
        path: Path to ``task.yaml``.
        project_task_defaults: Optional ``project.task_defaults`` dict layered in
            below the task's own fields (see :func:`load_task_yaml`).

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        RuntimeError: If a ``domain:`` ref cannot be resolved or the YAML is not
            a top-level mapping.
        pydantic.ValidationError: If the merged dict fails :class:`TaskConfig`
            validation.
    """
    task, _ = load_task_yaml(Path(path), project_task_defaults=project_task_defaults)
    return task


def agent_tool_configs(task: TaskConfig) -> dict[str, dict]:
    """Per-tool init kwargs lifted from the ``tools.agent.<name>`` blocks.

    Only enabled tools are read. The kwargs reach the runner via
    ``ToolSchema.tool_config`` and drive the builtin instantiation whose schema
    depends on them (``MobileTool(apps={…})`` populates
    ``actions[].app_name.enum``).

    Raises:
        ValueError: If a block is present but is not a mapping — an author typo
            that would otherwise surface at trial registration as a ``TypeError``
            with no task in the message.
    """
    configs: dict[str, dict] = {}
    for tool_name in task.tools.agent.get("enabled", []):
        raw = task.tools.agent.get(tool_name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(
                f"tools.agent.{tool_name} must be a mapping of init kwargs "
                f"(got {type(raw).__name__}={raw!r}) in task {task.task_id!r}"
            )
        configs[tool_name] = dict(raw)
    return configs


def resolve_agent_tool_schemas(
    task: TaskConfig, task_dir: Path, *, allow_subprocess: bool
) -> dict[str, dict]:
    """Resolve ``{tool_name: {"description", "parameters"}}`` for the agent's tools.

    An MCP task's schemas come from ``<task_dir>/fixtures/tools.json``; a task
    without an ``mcp_server`` gets them from the builtin registry.

    Args:
        task: The loaded task config.
        task_dir: The effective task dir, i.e. the second element of
            :func:`load_task_yaml`'s return.
        allow_subprocess: When ``True``, an MCP task with no readable fixture is
            resolved by starting its server and querying ``tools/list``, and the
            answer is cached into the task tree. When ``False`` the resolution is
            strictly read-only and that task resolves to no schemas at all — the
            mode a pre-run gate uses, because a gate must not mutate the tree it
            is validating.

    Raises:
        RuntimeError: If a live ``tools/list`` query is reached and fails.
        ValueError: If a ``tools.agent.<name>`` block is not a mapping.
    """
    enabled: list[str] = task.tools.agent.get("enabled", [])
    mcp_server_ref = task.tools.agent.get("mcp_server")
    if mcp_server_ref:
        return _load_rich_tool_schemas(
            task_dir, task_dir / mcp_server_ref, allow_subprocess=allow_subprocess
        )
    return _builtin_tool_schemas(enabled, agent_tool_configs(task))


def build_tool_inventory(task: TaskConfig, task_dir: Path) -> ToolInventory:
    """The task's declared tool set and resolved schemas, without touching the tree.

    A tool whose schema does not resolve — an MCP task with no committed
    ``fixtures/tools.json``, a name the builtin registry does not know, a fixture
    entry carrying no ``parameters`` mapping — is absent from
    :attr:`ToolInventory.parameters` and so classifies as
    :attr:`ArgumentSchema.UNKNOWN`. A resolved schema the task does not declare —
    an MCP fixture listing the harness's own state tool — is dropped too, so one
    grading block cannot draw an undeclared-tool error and an argument finding
    from the same name.
    """
    declared = frozenset(task.tools.agent.get("enabled", [])) | frozenset(
        task.tools.user.get("enabled", [])
    )
    schemas = resolve_agent_tool_schemas(task, task_dir, allow_subprocess=False)
    parameters = {
        name: entry["parameters"]
        for name, entry in schemas.items()
        if name in declared and isinstance(entry.get("parameters"), dict)
    }
    if "search_kb" in declared:
        # The runner rebuilds search_kb as a source-less RAG wrapper, so the
        # canonical schema is what the agent is handed whatever a fixture or the
        # registry holds. ``NativeAdapter.to_task_description`` reads the same
        # function for the whole wire object, whose category, timeout and absent
        # source this projection cannot carry.
        from tolokaforge.runner.tool_factory import create_search_kb_schema

        parameters["search_kb"] = create_search_kb_schema().parameters
    return ToolInventory(declared=declared, parameters=parameters, known=True)


def tool_inventory_under_adapter(
    task: TaskConfig, task_dir: Path, adapter_type: str
) -> ToolInventory:
    """The inventory to check *task*'s grading against, under *adapter_type*.

    ``tools.agent`` is the native reading of a task's tool set. For a task an
    external adapter owns it is not necessarily the set the run builds, so
    :meth:`ToolInventory.unresolvable` is the honest answer — checking names
    against a reading the adapter does not use would reject packs that run fine.
    """
    from tolokaforge.runner.models import AdapterType

    if adapter_type != AdapterType.NATIVE.value:
        return ToolInventory.unresolvable()
    return build_tool_inventory(task, task_dir)


def replay_world_under_adapter(task: TaskConfig, adapter_type: str) -> ReplayWorld:
    """What *task* gives a golden-action replay, under *adapter_type*.

    ``initial_state.json_db`` and ``tools.agent.mcp_server`` are the native reading of
    the world a replay executes in, so a task an external adapter owns gets
    :meth:`ReplayWorld.unresolvable` for the reason
    :func:`tool_inventory_under_adapter` gives: reporting a reading the adapter does not
    use would reject packs that run fine.

    ``mcp_server`` is read the way :meth:`BaseAdapter.grade` reads it into the grading
    engine, and the ``json_db`` shape through :func:`classify_initial_state`, the reading
    :func:`require_golden_replay_world` builds a world by at grade time — so an inline
    mapping supplies no file here either.
    """
    from tolokaforge.runner.models import AdapterType

    if adapter_type != AdapterType.NATIVE.value:
        return ReplayWorld.unresolvable()
    return ReplayWorld(
        initial_state=classify_initial_state(task.initial_state.json_db),
        mcp_server=bool(task.tools.agent.get("mcp_server")) if task.tools.agent else False,
    )


class GradingSourceKind(str, Enum):
    """Where a run of one task reads its grading block, or why it reads none."""

    ON_DISK = "on_disk"
    """The task names a grading file, and the run reads its block from there."""

    WITHHELD = "withheld"
    """The task names none, and the adapter it declares grades from one."""

    UNINTERROGABLE = "uninterrogable"
    """The task names none, and the adapter it declares answers for itself."""


@dataclass(frozen=True)
class GradingSource:
    """The grading file one task's run reads, or the sentence saying why there is none.

    An absence is two states, not one, because they decide opposite things: a task
    whose declared adapter grades from a file and supplies none cannot be graded at
    all, while a task whose declared adapter resolves its own is one nothing here
    can pronounce on.
    """

    kind: GradingSourceKind

    path: Path | None
    """The file, resolved against the task dir; ``None`` for both absences."""

    reason: str = ""
    """What the absence is, in the voice its channel is read in; empty on a resolved file.

    The withheld sentence is printed as a refusal and carries the task and both fixes,
    because no caller supplies them both: the CLI's line names the file rather than the
    task, and neither caller repeats a fix. The uninterrogable one travels the
    ``unchecked`` channel, whose own line names the task, so it reads like its neighbours
    there and names none.
    """

    def __post_init__(self) -> None:
        if (self.path is None) == (self.kind is GradingSourceKind.ON_DISK):
            raise ValueError(
                f"a grading source of kind {self.kind.value} carries path={self.path!r}: an "
                "on-disk source is the file a run reads, and every other kind has none"
            )
        if bool(self.reason) == (self.kind is GradingSourceKind.ON_DISK):
            raise ValueError(
                f"a grading source of kind {self.kind.value} carries reason={self.reason!r}: "
                "an absence is reported by the sentence naming it, and a resolved file needs "
                "no sentence"
            )


def grading_source_under_adapter(
    task: TaskConfig, task_dir: Path, adapter_type: str
) -> GradingSource:
    """Where *task*'s grading block is read from, under *adapter_type*.

    A task naming a grading file is :attr:`GradingSourceKind.ON_DISK` whatever the
    adapter — the path is joined, never stat'd, so a declared file that is not there
    is the reading gate's question and not this one's.

    A task naming none is answered off *adapter_type*, because
    :meth:`BaseAdapter.get_grading_config` is abstract and the implementations
    disagree: :class:`NativeAdapter` refuses such a task, while an external adapter
    may synthesise a whole config without reading the field.

    Which adapter that is, is the caller's to resolve, and the two gates resolve it
    differently on purpose. ``validate`` builds no description and reads the type the
    ``task.yaml`` declares. The pre-run gate reads the type the description it is
    about to run carries, which is the adapter that will actually be asked for a
    grading config — so a pack declaring an external adapter but loaded natively is
    refused there and passed by ``validate``, since the native adapter is the one that
    would have to grade it.
    """
    from tolokaforge.runner.models import AdapterType

    if task.grading is not None:
        return GradingSource(kind=GradingSourceKind.ON_DISK, path=task_dir / task.grading)
    if adapter_type != AdapterType.NATIVE.value:
        return GradingSource(
            kind=GradingSourceKind.UNINTERROGABLE,
            path=None,
            reason=(
                "this task declares no grading source — no `grading:` field and no sibling "
                f"grading.yaml — and adapter {adapter_type!r} decides for itself what a task "
                "with none grades by, so whether that absence is an authoring defect is not "
                "checkable here"
            ),
        )
    return GradingSource(
        kind=GradingSourceKind.WITHHELD,
        path=None,
        reason=(
            f"task {task.task_id!r} declares no grading source: no `grading:` field and no "
            f"sibling grading.yaml. Adapter {adapter_type!r} grades from that file, so this "
            "task cannot be graded and is refused before any trial is scheduled. "
            "Declare `grading:` pointing at the block this task grades by, or add a "
            "grading.yaml beside its task.yaml."
        ),
    )


def _builtin_tool_schemas(
    tool_names: list[str], tool_configs: dict[str, dict] | None = None
) -> dict[str, dict]:
    """Return rich schemas for the builtin tools among *tool_names*.

    Each known name is instantiated with kwargs from ``tool_configs[name]`` and
    its ``get_schema()`` output reduced to ``{"description", "parameters"}`` so
    the LLM receives real parameter descriptions. A name the registry does not
    know, or one whose construction raises, is absent from the result.
    """
    from tolokaforge.tools.builtin import registry

    configs = tool_configs or {}
    schemas: dict[str, dict] = {}
    for name in tool_names:
        if not registry.is_builtin(name):
            continue
        try:
            cls = registry.get_class(name)
            tool = cls(**configs.get(name, {}))
            raw = tool.get_schema()
            # get_schema() returns {"type": "function", "function": {…}}
            func_def = raw.get("function", raw)
            schemas[name] = {
                "description": func_def.get("description", f"Builtin tool: {name}"),
                "parameters": func_def.get("parameters", {"type": "object", "properties": {}}),
            }
        except Exception as exc:
            logger.debug("Could not load builtin schema", tool_name=name, error=str(exc))
    return schemas


def _load_rich_tool_schemas(
    task_dir: Path, mcp_server_path: Path, *, allow_subprocess: bool
) -> dict[str, dict]:
    """Load an MCP task's tool schemas, preferring the committed fixture.

    ``task_dir/fixtures/tools.json`` is read first. When it is absent or
    unreadable, a live ``tools/list`` query fills the gap and its answer is
    cached back into the fixture — but only under *allow_subprocess*; without it
    the unresolved task yields ``{}`` and neither the server nor the tree is
    touched.
    """
    tools_json_path = task_dir / "fixtures" / "tools.json"

    if tools_json_path.exists():
        try:
            with open(tools_json_path) as f:
                tools_list = json.load(f)
            schemas = {t["name"]: t for t in tools_list if isinstance(t, dict) and "name" in t}
            logger.info(
                "Loaded tool schemas from fixtures/tools.json",
                count=len(schemas),
                path=str(tools_json_path),
            )
            return schemas
        except Exception as exc:
            logger.warning(
                "Failed to read fixtures/tools.json",
                path=str(tools_json_path),
                error=str(exc),
            )

    if not allow_subprocess:
        return {}

    schemas = _fetch_mcp_tool_schemas(mcp_server_path)
    try:
        tools_json_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tools_json_path, "w") as f:
            json.dump(list(schemas.values()), f, indent=2)
        logger.info(
            "Generated and cached fixtures/tools.json",
            path=str(tools_json_path),
            count=len(schemas),
        )
    except Exception as exc:
        logger.warning(
            "Could not write fixtures/tools.json cache",
            path=str(tools_json_path),
            error=str(exc),
        )
    return schemas


def _fetch_mcp_tool_schemas(mcp_server_path: Path) -> dict[str, dict]:
    """Fetch tool schemas from an MCP server via ``tools/list``.

    Starts the server as a subprocess, performs the MCP handshake, calls
    ``tools/list``, and converts the response to the ``fixtures/tools.json``
    format — MCP's ``inputSchema`` becomes ``parameters``, matching what the
    ``tlk_mcp_core`` adapter writes.

    Raises:
        RuntimeError: If the server fails to start, the handshake fails,
            ``tools/list`` errors, or the server returns no tools. A silent
            ``{}`` here reaches the LLM as parameter-less tool stubs and reads
            as an agent-reasoning bug.
    """
    from tolokaforge.runner.tool_factory import MCPServerProcess

    try:
        server = MCPServerProcess(script_path=str(mcp_server_path))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to construct MCPServerProcess for {mcp_server_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    try:
        server.start()
        result = server.send_request("tools/list", {})
    except Exception as exc:
        raise RuntimeError(
            f"Failed to query tools/list from MCP server {mcp_server_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            server.stop()
        except Exception as stop_exc:
            logger.warning(
                "MCP server stop failed after tools/list query",
                server=str(mcp_server_path),
                error=str(stop_exc),
            )

    schemas: dict[str, dict] = {}
    for tool in result.get("tools", []):
        name = tool.get("name", "")
        if not name:
            continue
        schemas[name] = {
            "name": name,
            "description": tool.get("description", f"Tool: {name}"),
            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
        }
    if not schemas:
        raise RuntimeError(
            f"MCP server {mcp_server_path} returned no tools from tools/list "
            f"(response keys: {list(result.keys())!r})"
        )
    logger.info(
        "Fetched MCP tool schemas",
        count=len(schemas),
        server=str(mcp_server_path),
    )
    return schemas


def _resolve_environment_manifest_paths(task_data: dict, task_root: Path, task_path: Path) -> None:
    """Rewrite ``environment_manifest.stack.compose_file`` (canonical)
    or the legacy flat ``environment_manifest.compose_file`` to an
    absolute path when the value is a task-relative string. In-place
    edit on the ``task_data`` dict before Pydantic constructs
    :class:`TaskConfig`, so ``EnvironmentPatch``'s legacy-shape
    normaliser sees an anchored path.

    Raises :class:`RuntimeError` with the offending ``task_path`` when
    the field is present but shaped wrong — matches the loader's
    fail-loud pattern for corrupt YAML instead of deferring to a
    generic Pydantic error at ``TaskConfig`` construction, which loses
    the file / field context.
    """
    if "environment_manifest" not in task_data:
        return
    manifest = task_data["environment_manifest"]
    if not isinstance(manifest, dict):
        raise RuntimeError(
            f"Task file {task_path}: 'environment_manifest' must be a YAML mapping "
            f"(got {type(manifest).__name__})"
        )

    if "stack" in manifest and manifest["stack"] is None:
        with source_context(task_path):
            warn_deprecated(
                legacy="'environment_manifest.stack: null'",
                canonical="omit the key or declare a stack sub-object",
                detail=(
                    "A task cannot unset the environment out from under a project "
                    "that declares one. Omit the key entirely to inherit the project's "
                    "stack, or declare a stack sub-object explicitly."
                ),
                stacklevel=2,
            )
        # Drop the null key so the loader treats it as unset (inherit-from-project).
        manifest.pop("stack")

    stack = manifest.get("stack")
    if isinstance(stack, dict) and "compose_file" in stack:
        compose_file = stack["compose_file"]
        if compose_file is None:
            with source_context(task_path):
                warn_deprecated(
                    legacy="'environment_manifest.stack.compose_file: null'",
                    canonical="omit the key or declare a compose_file path",
                    detail=(
                        "A task cannot unset the substrate pointer; there is no "
                        "engine-default compose file to fall through to. Omit the "
                        "key entirely to inherit the project's compose_file."
                    ),
                    stacklevel=2,
                )
            # Drop the null key so the loader treats it as unset (inherit-from-project).
            stack.pop("compose_file")
        else:
            if not isinstance(compose_file, str):
                raise RuntimeError(
                    f"Task file {task_path}: "
                    f"'environment_manifest.stack.compose_file' must be a string "
                    f"(got {type(compose_file).__name__})"
                )
            resolved = Path(compose_file)
            if not resolved.is_absolute():
                resolved = (task_root / resolved).resolve()
            stack["compose_file"] = str(resolved)

    if "compose_file" in manifest:
        compose_file = manifest["compose_file"]
        if not isinstance(compose_file, str):
            raise RuntimeError(
                f"Task file {task_path}: 'environment_manifest.compose_file' must be a "
                f"string (got {type(compose_file).__name__})"
            )
        resolved = Path(compose_file)
        if not resolved.is_absolute():
            resolved = (task_root / resolved).resolve()
        manifest["compose_file"] = str(resolved)


def _detect_task_root(task_path: Path) -> Path:
    """Return the effective task directory for a discovered ``task.yaml``.

    For the shared-domain layout ``<domain>/testcases/<case>/task.yaml`` the
    task root is ``<domain>`` so shared files (e.g. ``_shared/mcp_server.py``)
    are bundled alongside case-specific files. For the legacy flat layout
    ``<task>/task.yaml`` the task root is the parent of ``task.yaml``.
    """
    parent = task_path.parent
    if parent.parent.name == "testcases":
        return parent.parent.parent
    return parent


def _load_domain_dict(task_path: Path, task_data: dict, task_root: Path) -> dict:
    """Return the ``domain:`` ref's contents with paths rewritten into the
    task-root frame. Returns ``{}`` when the task has no ``domain`` ref.

    The caller is responsible for merging the returned dict into the
    task's own dict. Splitting load from merge lets the caller layer
    additional sources (e.g. ``project.task_defaults``) between the
    domain bundle and the task's own fields, with the correct
    precedence.
    """
    domain_ref = task_data.get("domain")
    if not domain_ref or not isinstance(domain_ref, str):
        return {}

    domain_path = (task_path.parent / domain_ref).resolve()
    if not domain_path.exists():
        raise RuntimeError(
            f"Domain file referenced by {task_path} not found: {domain_path} "
            f"(ref: {domain_ref!r}) — fix the 'domain:' path in the task.yaml"
        )

    try:
        with open(domain_path) as f:
            domain_data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise RuntimeError(
            f"Failed to read domain file {domain_path} referenced by {task_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(domain_data, dict):
        raise RuntimeError(
            f"Domain file {domain_path} referenced by {task_path} is not a YAML "
            f"mapping (got {type(domain_data).__name__})"
        )

    _rewrite_task_paths(domain_data, domain_path.parent, task_root)
    # Lift legacy top-level ``user_simulator`` inside the domain layer to
    # ``actors.user`` here (rather than at the caller) so any
    # ``DeprecationWarning`` names this domain file, not the referring
    # task.yaml. Idempotent: a canonical layer is left untouched.
    with source_context(domain_path):
        canonicalize_actor_config(domain_data)
    return domain_data


def _rewrite_path(value: str, from_dir: Path, to_dir: Path) -> str:
    """Express *value* (interpreted relative to *from_dir*) as a path relative
    to *to_dir*. Raises :class:`ValueError` if it is not expressible (e.g.
    cross-drive on Windows)."""
    abs_path = (from_dir / value).resolve()
    try:
        return Path(os.path.relpath(abs_path, to_dir.resolve())).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"Cannot express {abs_path} relative to {to_dir.resolve()} "
            f"(original value {value!r} from {from_dir}). "
            "Paths must live inside the domain root."
        ) from exc


# Declarative list of relative-path fields in a task.yaml mapping. Each entry
# is (selector, applier). The selector is a dotted path of dict keys; the
# applier knows how to walk and rewrite the leaf — most fields are simple
# strings, but ``initial_state.filesystem.copy`` is a list of dicts where
# only the ``from`` key is a path.
_FieldRewriter = Callable[[dict, Callable[[str], str]], None]


def _rewrite_string_field(*keys: str) -> _FieldRewriter:
    """Build a rewriter that walks ``data[k1][k2]…[kn]`` and rewrites it if
    the leaf is a non-empty string."""

    def rewriter(data: dict, rewrite: Callable[[str], str]) -> None:
        node: Any = data
        for key in keys[:-1]:
            if not isinstance(node, dict):
                return
            node = node.get(key)
            if node is None:
                return
        if not isinstance(node, dict):
            return
        leaf = keys[-1]
        val = node.get(leaf)
        if isinstance(val, str) and val:
            node[leaf] = rewrite(val)

    return rewriter


def _rewrite_filesystem_copy(data: dict, rewrite: Callable[[str], str]) -> None:
    """Walk ``initial_state.filesystem.copy[].from`` (list of dicts)."""
    initial = data.get("initial_state")
    if not isinstance(initial, dict):
        return
    fs = initial.get("filesystem")
    if not isinstance(fs, dict):
        return
    copy_spec = fs.get("copy")
    if not isinstance(copy_spec, list):
        return
    for entry in copy_spec:
        if not isinstance(entry, dict):
            continue
        src = entry.get("from")
        if isinstance(src, str) and src:
            entry["from"] = rewrite(src)


_PATH_FIELD_REWRITERS: tuple[_FieldRewriter, ...] = (
    _rewrite_string_field("grading"),
    _rewrite_string_field("system_prompt"),
    _rewrite_string_field("tools", "agent", "mcp_server"),
    _rewrite_string_field("tools", "user", "mcp_server"),
    _rewrite_string_field("initial_state", "json_db"),
    _rewrite_string_field("initial_state", "system_prompt"),
    _rewrite_filesystem_copy,
)


def _rewrite_task_paths(task_data: dict, from_dir: Path, to_dir: Path) -> None:
    """Rewrite every relative-path field in *task_data* from *from_dir*-relative
    to *to_dir*-relative.

    Mutates *task_data* in place. Path fields are enumerated by
    :data:`_PATH_FIELD_REWRITERS` rather than detected heuristically — adding a
    new path field to :class:`TaskConfig` requires a one-line addition there.

    ``initial_state.json_db`` is only rewritten when it is a string. The dict
    form is an inline state literal and has no path to resolve.
    """

    def rewrite(val: str) -> str:
        return _rewrite_path(val, from_dir, to_dir)

    for rewriter in _PATH_FIELD_REWRITERS:
        rewriter(task_data, rewrite)
