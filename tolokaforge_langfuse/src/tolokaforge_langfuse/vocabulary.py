"""The default trace vocabulary of a tolokaforge trial (ADR-0047, vocabulary amendment).

What a trial's trace can be filtered by in the receiver is fixed here, once, for both producers
(the live observer and the offline bundle uploader): the tag prefixes, which of them the
producer sets from the bundle and which a caller supplies, the closed value lists the engine's
own output format defines, the tags derived from the model-name normalizer's fields and from
the bundle (``task.yaml`` ``model_config.agent``, ``metrics.yaml`` ``usage.calls``), and the
receiver's native ``environment`` rule. A deployment narrows and extends the *values* in its
profile (:mod:`tolokaforge_langfuse.profile`), never the prefixes: a tag prefix nobody can query
by is permanent noise, since the receiver merges tags as a set and never removes one.

Nothing here names a deployment: the words are the engine's (``run_kind``, ``scope``, ``task``),
the model's (the normalizer's facets) or the receiver's (``environment``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# the prefixes, in the order a trace lists them (a producer artefact; receivers compare sets)
CORE_PREFIXES: tuple[str, ...] = (
    "team",
    "project",
    "dataset",
    "harness",
    "source",
    "model",
    "model_vendor",
    "model_family",
    "model_generation",
    "model_tier",
    "model_variant",
    "model_size",
    "model_stage",
    "model_snapshot",
    "run_kind",
    "scope",
    "config",
    "domain",
    "task",
    "ci_run",
    "ci_chain",
    "reasoning_mode",
    "reasoning_effort",
    "reasoning_budget",
    "route",
)
# set by the producer from what it knows (the receiver's project, the harness, the source, the
# bundle's task, the model identity and its facets, the agent's reasoning settings and route);
# a caller may not supply them
PRODUCER_PREFIXES: frozenset[str] = frozenset(
    {
        "project",
        "harness",
        "source",
        "task",
        "model",
        "model_vendor",
        "model_family",
        "model_generation",
        "model_tier",
        "model_variant",
        "model_size",
        "model_stage",
        "model_snapshot",
        "reasoning_mode",
        "reasoning_effort",
        "reasoning_budget",
        "route",
    }
)
CALLER_PREFIXES: tuple[str, ...] = tuple(p for p in CORE_PREFIXES if p not in PRODUCER_PREFIXES)
# the prefix the launcher that owns the receiver injects (the destination's project); the one
# producer prefix a caller may pass, because the producer checks it against the receiver
LAUNCHER_PREFIXES: frozenset[str] = frozenset({"project"})
# closed lists the engine's own output format defines; a profile narrows them, never widens
CORE_VALUES: dict[str, tuple[str, ...]] = {
    "run_kind": ("eval", "smoke", "canary", "test", "probe"),
    "scope": ("full", "sample"),
}
# required on every trace, whatever its source, and additionally on an evaluation trial
REQUIRED_ALWAYS: tuple[str, ...] = ("team", "project", "harness", "source", "run_kind")
REQUIRED_FOR_TRIAL: tuple[str, ...] = ("dataset", "scope", "task")

HARNESS = "tolokaforge"
SOURCE_TRIAL = "trial"
SOURCE_TRANSCRIPT = "agent-transcript"
# the caller prefixes that describe an agent transcript (no dataset, scope, domain, config or
# task on a transcript; they describe a trial)
TRANSCRIPT_CALLER_PREFIXES: tuple[str, ...] = ("team", "run_kind", "ci_run", "ci_chain")

# the receiver's native environment: production for a real evaluation, development otherwise
PRODUCTION_RUN_KINDS: frozenset[str] = frozenset({"eval"})
ENVIRONMENT_PRODUCTION = "production"
ENVIRONMENT_DEVELOPMENT = "development"

# the normalizer's descriptive facets that become ``model_<facet>`` tags when the rules derive a
# value (vendor and family ride in the normalizer's own tags)
MODEL_FACETS: tuple[str, ...] = ("generation", "tier", "variant", "size", "stage", "snapshot")
# the groups of tags derived from the bundle; a profile may switch a group off
DERIVED_GROUP_MODEL_FACETS = "model_facets"
DERIVED_GROUP_REASONING = "reasoning"
DERIVED_GROUP_ROUTE = "route"
DERIVED_GROUPS: tuple[str, ...] = (
    DERIVED_GROUP_MODEL_FACETS,
    DERIVED_GROUP_REASONING,
    DERIVED_GROUP_ROUTE,
)
ALL_DERIVED_GROUPS: frozenset[str] = frozenset(DERIVED_GROUPS)
# the route value when every model call of the trial went through the LiteLLM gateway (the
# bundle records it per call as ``cost_source``); the config's provider otherwise
ROUTE_GATEWAY = "litellm"
COST_SOURCE_GATEWAY = "litellm"

_PREFIX_SHAPE = re.compile(r"^[a-z][a-z0-9_]*$")
_VALUE_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+@:-]*$")
MAX_VALUE_CHARS = 128


class VocabularyError(ValueError):
    """A tag outside the vocabulary; the message names the tag, never a credential."""


def split_tag(tag: object) -> tuple[str, str]:
    """``<prefix>:<value>`` -> (prefix, value); anything else is a VocabularyError."""
    if not isinstance(tag, str):
        raise VocabularyError(f"tag {tag!r} must be a string of the form <prefix>:<value>")
    prefix, sep, value = tag.partition(":")
    if not sep or not prefix or not value:
        raise VocabularyError(f"tag {tag!r} must look like <prefix>:<value>")
    if not _PREFIX_SHAPE.match(prefix):
        raise VocabularyError(f"tag {tag!r}: prefix must be lowercase letters, digits or '_'")
    check_value(prefix, value)
    return prefix, value


def check_value(prefix: str, value: str) -> str:
    if len(value) > MAX_VALUE_CHARS or not _VALUE_SHAPE.match(value):
        raise VocabularyError(
            f"tag {prefix}:{value!r}: the value must be 1-{MAX_VALUE_CHARS} characters of letters, "
            "digits and . _ / + @ : - without whitespace"
        )
    return value


def is_valid_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_VALUE_CHARS
        and bool(_VALUE_SHAPE.match(value))
    )


def check_caller_prefix(prefix: str, *, where: str = "tag") -> str:
    """A prefix a caller (or a profile) may speak about: known, and not the producer's."""
    if prefix not in CORE_PREFIXES:
        raise VocabularyError(
            f"{where}: {prefix!r} is not a core prefix; the core prefixes are {list(CORE_PREFIXES)}"
        )
    if prefix in PRODUCER_PREFIXES:
        raise VocabularyError(f"{where}: {prefix!r} is set by the producer and cannot be supplied")
    return prefix


def allowed_values(
    prefix: str, profile_values: Mapping[str, Sequence[str]] | None = None
) -> tuple[str, ...] | None:
    """The closed list for ``prefix``: the profile's when it has one, else the core's, else None."""
    listed = (profile_values or {}).get(prefix)
    if listed:
        return tuple(listed)
    return CORE_VALUES.get(prefix)


def validate_caller_tag(
    tag: object,
    *,
    profile_values: Mapping[str, Sequence[str]] | None = None,
    launcher: bool = False,
) -> str:
    """A tag a caller supplies: well-formed, a caller prefix (``launcher`` admits the receiver's
    project tag too), and a value inside the closed list when the prefix has one."""
    prefix, value = split_tag(tag)
    if not (launcher and prefix in LAUNCHER_PREFIXES):
        check_caller_prefix(prefix, where=f"tag {tag!r}")
    allowed = allowed_values(prefix, profile_values)
    if allowed is not None and value not in allowed:
        raise VocabularyError(f"tag {tag!r}: the value must be one of {list(allowed)}")
    return tag  # type: ignore[return-value]


def order_tags(tags: Iterable[str]) -> list[str]:
    """Deduplicated, in core order (prefixes unknown to the core last, in first-seen order)."""
    seen: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    rank = {prefix: index for index, prefix in enumerate(CORE_PREFIXES)}
    return sorted(seen, key=lambda t: (rank.get(t.partition(":")[0], len(rank)), seen.index(t)))


def facet_tags(fields: Mapping[str, Any] | None) -> list[str]:
    """``model_<facet>:<value>`` for every normalizer facet the rules derived a value for."""
    tags: list[str] = []
    for facet in MODEL_FACETS:
        value = (fields or {}).get(facet)
        if value in (None, ""):
            continue
        text = str(value)
        if is_valid_value(text):
            tags.append(f"model_{facet}:{text}")
    return tags


def agent_config(task: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The agent's ``model_config`` block of ``task.yaml``; a harness bundle names the harness as
    a string and records the model next to it under ``model_info``."""
    config = (task or {}).get("model_config")
    if not isinstance(config, Mapping):
        return {}
    agent = config.get("agent")
    if isinstance(agent, str):
        agent = config.get("model_info")
    return agent if isinstance(agent, Mapping) else {}


def derived_tags(
    task: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | None,
    *,
    groups: Iterable[str] = DERIVED_GROUPS,
) -> list[str]:
    """The tags a trial's bundle implies beyond the model identity: the agent's reasoning
    settings (``reasoning_mode``, ``reasoning_effort``, ``reasoning_budget``) and the route the
    calls took (``route``: the configured provider, or the gateway when every call's
    ``cost_source`` names it). Nothing is invented: an absent or malformed fact yields no tag."""
    wanted = set(groups)
    agent = agent_config(task)
    tags: list[str] = []
    if DERIVED_GROUP_REASONING in wanted:
        reasoning = agent.get("reasoning")
        if isinstance(reasoning, Mapping):
            for prefix, key in (
                ("reasoning_mode", "mode"),
                ("reasoning_effort", "effort_hint"),
                ("reasoning_budget", "budget_tokens"),
            ):
                value = reasoning.get(key)
                if value in (None, "", False):
                    continue
                text = str(value)
                if is_valid_value(text):
                    tags.append(f"{prefix}:{text}")
    if DERIVED_GROUP_ROUTE in wanted:
        route = agent.get("provider")
        calls = ((metrics or {}).get("usage") or {}).get("calls")
        if (
            isinstance(calls, list)
            and calls
            and all(
                isinstance(c, Mapping) and c.get("cost_source") == COST_SOURCE_GATEWAY
                for c in calls
            )
        ):
            route = ROUTE_GATEWAY
        if route not in (None, "") and is_valid_value(str(route)):
            tags.append(f"route:{route}")
    return tags


@dataclass(frozen=True)
class EnvironmentRule:
    """``literal`` fixes the receiver's native environment; ``from_tag`` maps the value of one
    tag prefix through ``values`` and falls back to ``default`` (also when the tag is absent)."""

    literal: str | None = None
    from_tag: str | None = None
    default: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, tags: Sequence[str]) -> str | None:
        if self.literal is not None:
            return self.literal
        if self.from_tag is None:
            return None
        for tag in tags:
            prefix, _, value = tag.partition(":")
            if prefix == self.from_tag:
                return self.values.get(value, self.default)
        return self.default


# the default rule: a real evaluation is production data, everything else development
DEFAULT_ENVIRONMENT_RULE = EnvironmentRule(
    from_tag="run_kind",
    default=ENVIRONMENT_DEVELOPMENT,
    values=dict.fromkeys(sorted(PRODUCTION_RUN_KINDS), ENVIRONMENT_PRODUCTION),
)


def environment_for(tags: Sequence[str]) -> str:
    """The default rule over a trace's tags (``production`` for ``run_kind:eval``)."""
    return DEFAULT_ENVIRONMENT_RULE.resolve(tags) or ENVIRONMENT_DEVELOPMENT
