"""Generation-parameter adaptation for a target model.

:class:`GenerationParams` is the :class:`ParamsPolicy` implementation shipped
today — it composes temperature / seed / reasoning kwargs per-model.

Extension contract
------------------
:class:`ParamsPolicy` is the base class for every generation-parameter
policy the engine dispatches to via the ``params_policy`` preset slot.
Every subclass **must** declare ``KNOWN_KEYS: ClassVar[frozenset[str]]``
enumerating the construction kwargs it accepts — this is the authoritative
source of truth for overlay preset-params validation (see
:func:`tolokaforge.core.llm.presets._params_slot_known_keys`). A subclass
that omits ``KNOWN_KEYS`` raises :class:`TypeError` at class-body
evaluation.

Reasoning routing
-----------------
``ReasoningConfig`` declaratively picks which provider kwargs get emitted.
Four preset-driven knobs on :class:`GenerationParams` control the routing:

* ``reasoning_via_thinking_kwarg`` — Anthropic-native budget mode. Emits the
  canonical litellm top-level ``thinking={"type":"enabled","budget_tokens":N}``
  kwarg when ``ReasoningConfig.mode == "budget"``. Used only on the *direct*
  Anthropic transport — OpenRouter silently drops this kwarg.
* ``reasoning_via_extra_body`` — OpenRouter transport. Emits
  ``extra_body.reasoning={…, "enabled": True}``: ``{"effort": <hint>}``
  for ``adaptive`` mode and ``{"max_tokens": N}`` for ``budget`` mode.
  When set alongside ``reasoning_via_thinking_kwarg`` (Anthropic preset +
  OpenRouter overlay) ``extra_body`` wins because the actual transport is
  OpenRouter.
* ``drop_sampling_when_thinking`` — when a reasoning kwarg is actually
  emitted (either transport), drop ``temperature`` / ``top_p`` / ``top_k``.
  Anthropic strips these whenever thinking is active.
* ``reasoning_budget_default`` — preset-level default for ``budget_tokens``
  when ``ReasoningConfig(mode="budget")`` is passed bare.

Routing table
~~~~~~~~~~~~~

+----------------+--------------------------+--------------------------+---------------------+
| ``mode``       | extra-body (OpenRouter)  | thinking-kwarg (direct)  | plain preset        |
+================+==========================+==========================+=====================+
| ``off``        | (nothing)                | (nothing)                | (nothing)           |
+----------------+--------------------------+--------------------------+---------------------+
| ``adaptive``   | ``extra_body.reasoning`` | **ValueError**           | ``reasoning_effort``|
|                | ``{effort,enabled}``     |                          |                     |
+----------------+--------------------------+--------------------------+---------------------+
| ``budget``     | ``extra_body.reasoning`` | ``thinking={…}``         | effort fallback     |
|                | ``{max_tokens,enabled}`` |                          |                     |
+----------------+--------------------------+--------------------------+---------------------+

See [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md) § ``params_policy``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Final

from tolokaforge.core.llm.reasoning import ReasoningConfig

__all__ = [
    "ParamsPolicy",
    "ParamPolicy",  # noqa: F822 — resolved via module-level __getattr__ shim
    "GenerationParams",
    # Named in docs/LLM_LAYER.md as the operator-facing contract, so exported
    # rather than left as module internals a reader cannot import.
    "RULABLE_PARAMS",
    "RuleAction",
    "VALID_RULE_ACTIONS",
]

logger = logging.getLogger(__name__)

_SAMPLING_KEYS: tuple[str, ...] = ("temperature", "top_p", "top_k")


class ParamsPolicy(ABC):
    """Adapts generation parameters for the target model.

    Subclasses declare ``KNOWN_KEYS`` — a frozen set of the construction
    kwargs they accept. The overlay validator reads the union of every
    registered subclass's ``KNOWN_KEYS`` to decide which preset ``params:``
    keys are legal, so adding a knob is a one-line declarative change on
    the subclass rather than an engine-wide inspection point.
    """

    KNOWN_KEYS: ClassVar[frozenset[str]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "KNOWN_KEYS" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare KNOWN_KEYS: ClassVar[frozenset[str]] "
                f"listing the construction kwargs it accepts. KNOWN_KEYS is the "
                f"authoritative source of truth for overlay preset-params "
                f"validation; a silent omission would let unknown YAML keys "
                f"reach __init__ and raise TypeError far from the operator."
            )

    @abstractmethod
    def adapt(
        self,
        kwargs: dict[str, Any],
        config_temperature: float | None,
        config_seed: int | None,
        config_reasoning: ReasoningConfig,
        temperature: float | None,
        seed: int | None,
        reasoning: ReasoningConfig | None,
    ) -> dict[str, Any]: ...

    def rule_for(self, param: str, value: str | None) -> str | None:
        """Declared action for ``value`` of ``param``; ``None`` when unruled.

        Concrete on the base rather than abstract: ``LLMClient`` asks every
        params policy about ``tool_choice``, and a policy that declares no
        value gaps should not have to implement a method to say so.
        """
        return None

    def rule_evidence(self, param: str, value: str | None) -> str | None:
        """Evidence behind a declared value gap; ``None`` when unruled."""
        return None

    def rule_substitute(self, param: str, value: str | None) -> str | None:
        """Replacement value for an ``override`` rule; ``None`` otherwise."""
        return None


#: Deprecated alias for :class:`ParamsPolicy`. Kept as a class-identity
#: alias (``ParamPolicy is ParamsPolicy`` remains true) so
#: ``isinstance()`` / ``issubclass()`` checks against either name continue
#: to work. Module-level ``__getattr__`` below emits a one-off
#: :class:`DeprecationWarning` on the old name; direct references to
#: :class:`ParamsPolicy` are silent. Shim removed in v0.18.0.
_LEGACY_PARAM_POLICY_WARNED: set[str] = set()


def __getattr__(name: str) -> Any:
    if name == "ParamPolicy":
        if "ParamPolicy" not in _LEGACY_PARAM_POLICY_WARNED:
            import warnings

            warnings.warn(
                "tolokaforge.core.llm.params_policy.ParamPolicy is "
                "deprecated; import "
                "tolokaforge.core.llm.params_policy.ParamsPolicy instead. "
                "Shim removed in v0.18.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            _LEGACY_PARAM_POLICY_WARNED.add("ParamPolicy")
        return ParamsPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True)
class _ValueRule:
    """One declared value gap: what to do, and the evidence it rests on."""

    action: RuleAction
    evidence: str
    substitute: str | None = None


def _normalise_value_rules(
    raw: dict[str, dict[str, dict[str, str]]] | None,
) -> dict[tuple[str, str], _ValueRule]:
    """Validate and flatten a ``param_value_rules`` block.

    Every rejection here is a preset that would otherwise look like it does
    something and quietly do nothing, so all of them raise rather than warn.
    """
    rules: dict[tuple[str, str], _ValueRule] = {}
    if raw is not None and not isinstance(raw, dict):
        raise ValueError(
            f"param_value_rules: expected a mapping of parameter -> value -> "
            f"rule, got {type(raw).__name__}."
        )
    for param, values in (raw or {}).items():
        if param not in RULABLE_PARAMS:
            raise ValueError(
                f"param_value_rules: {param!r} is not a rulable parameter "
                f"(known: {sorted(RULABLE_PARAMS)}). Nothing would ever read a "
                f"rule on it, so it would be accepted and silently do nothing."
            )
        if not isinstance(values, dict):
            raise ValueError(
                f"param_value_rules[{param!r}]: expected a mapping of "
                f"value -> rule, got {type(values).__name__}."
            )
        for value, spec in values.items():
            if not isinstance(spec, dict):
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: expected a "
                    f"mapping with 'action' and 'evidence', got "
                    f"{type(spec).__name__}."
                )
            unknown = set(spec) - {"action", "evidence", "with"}
            if unknown:
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: unknown key(s) "
                    f"{sorted(unknown)}. Legal keys are ['action', 'evidence', "
                    f"'with']. An unrecognised key here would be accepted and "
                    f"never read, which is what every other check in this "
                    f"function exists to prevent."
                )
            action = str(spec.get("action", "")).lower()
            if action not in VALID_RULE_ACTIONS:
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: action "
                    f"{spec.get('action')!r} is not one of "
                    f"{sorted(VALID_RULE_ACTIONS)}."
                )
            evidence = str(spec.get("evidence", "")).strip()
            if not evidence:
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: 'evidence' is "
                    f"required. A value gap is a claim about a provider on a "
                    f"date; without the claim written down nobody can tell "
                    f"later whether it still holds."
                )
            normalised_value = str(value).lower()
            substitute = spec.get("with")
            if action == RuleAction.OVERRIDE:
                if not str(substitute or "").strip():
                    raise ValueError(
                        f"param_value_rules[{param!r}][{value!r}]: action "
                        f"'override' requires a 'with' value — the rule has to "
                        f"say what to send instead."
                    )
                substitute = str(substitute).lower()
                if substitute == normalised_value:
                    raise ValueError(
                        f"param_value_rules[{param!r}][{value!r}]: 'with' is "
                        f"the value being overridden; that rule does nothing."
                    )
            elif substitute is not None:
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: 'with' is only "
                    f"meaningful for action 'override', not {action!r}."
                )
            rules[(param, normalised_value)] = _ValueRule(
                action=RuleAction(action), evidence=evidence, substitute=substitute
            )
    # A substitute that is itself ruled would send a value the same block
    # already declares unusable, so reject the contradiction rather than
    # resolve it in some order the operator cannot predict.
    for (param, value), rule in rules.items():
        if rule.substitute is not None and (param, rule.substitute) in rules:
            raise ValueError(
                f"param_value_rules[{param!r}][{value!r}]: 'with' names "
                f"{rule.substitute!r}, which this block also declares a rule "
                f"for. Substituting into another declared gap would send a "
                f"value already known to be unusable."
            )
    return rules


#: Parameters with a consult site, i.e. the ones a rule can actually reach. A
#: rule on anything else would be accepted and then never read, so it is
#: refused: that is a typo, not a decision. Adding a parameter here means
#: adding the site that consults it.
RULABLE_PARAMS: Final[frozenset[str]] = frozenset({"reasoning_effort", "tool_choice"})


class RuleAction(str, Enum):
    """What a rule may ask for.

    A named type rather than bare literals: the action is compared at three
    consult sites across two modules, and a typo at a future one
    (``action == "overide"``) would be a silent no-op — the exact failure class
    this feature exists to remove. Subclassing ``str`` keeps YAML values and
    equality against plain strings working.

    ``reject`` refuses to build the request. ``drop`` omits the parameter and
    lets the provider default apply. ``override`` sends a declared replacement.

    ``drop`` and ``override`` both change what the request carries, and only
    the caller knows whether that matters — omitting ``tool_choice`` is the
    provider's own spelling of ``auto`` and costs nothing, while omitting
    ``reasoning_effort`` yields the provider's default budget rather than the
    level asked for. The engine does not adjudicate: it applies the
    declaration and logs a WARNING naming both values, so a caller that
    compares results can see the deviation. ``docs/LLM_LAYER.md`` carries the
    warning in full.
    """

    REJECT = "reject"
    DROP = "drop"
    OVERRIDE = "override"


#: Derived, so the set and the type cannot drift apart.
VALID_RULE_ACTIONS: Final[frozenset[str]] = frozenset(a.value for a in RuleAction)


class GenerationParams(ParamsPolicy):
    """Adapts generation kwargs based on model constraints.

    Configurable once at construction; ``adapt()`` applies the rules on every
    ``generate()`` call.
    """

    KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "fixed_temperature",
            "supports_seed",
            "reasoning_via_extra_body",
            "reasoning_via_thinking_kwarg",
            "drop_sampling_when_thinking",
            "reasoning_budget_default",
            "param_value_rules",
        }
    )

    def __init__(
        self,
        fixed_temperature: float | None = None,
        supports_seed: bool = True,
        reasoning_via_extra_body: bool = False,
        reasoning_via_thinking_kwarg: bool = False,
        drop_sampling_when_thinking: bool = False,
        reasoning_budget_default: int | None = None,
        param_value_rules: dict[str, dict[str, dict[str, str]]] | None = None,
    ):
        self._fixed_temperature = fixed_temperature
        self._supports_seed = supports_seed
        self._reasoning_via_extra_body = reasoning_via_extra_body
        self._reasoning_via_thinking_kwarg = reasoning_via_thinking_kwarg
        self._drop_sampling_when_thinking = drop_sampling_when_thinking
        self._reasoning_budget_default = reasoning_budget_default
        # Declared value gaps, flattened to (param, value) -> rule. Populated
        # from a preset or a provider overlay; see ``docs/LLM_LAYER.md``
        # § param_value_rules for what each action means and when to reach for
        # which.
        self._param_value_rules: dict[tuple[str, str], _ValueRule] = _normalise_value_rules(
            param_value_rules
        )

    def adapt(
        self,
        kwargs: dict[str, Any],
        config_temperature: float | None,
        config_seed: int | None,
        config_reasoning: ReasoningConfig,
        temperature: float | None,
        seed: int | None,
        reasoning: ReasoningConfig | None,
    ) -> dict[str, Any]:
        # Temperature — caller override > fixed > config
        temp = temperature if temperature is not None else config_temperature
        if temp is not None:
            kwargs["temperature"] = temp
        if self._fixed_temperature is not None:
            kwargs["temperature"] = self._fixed_temperature

        # Seed
        if self._supports_seed:
            seed_value = seed if seed is not None else config_seed
            if seed_value is not None:
                kwargs["seed"] = seed_value

        # Reasoning — caller override > config
        active = reasoning if reasoning is not None else config_reasoning
        self._apply_reasoning(kwargs, active)

        return kwargs

    # ------------------------------------------------------------------
    # Reasoning mode dispatch
    # ------------------------------------------------------------------

    def _apply_reasoning(self, kwargs: dict[str, Any], cfg: ReasoningConfig) -> None:
        """Dispatch to the correct routing based on ``cfg.mode`` + preset flags.

        ``reasoning_via_extra_body`` (OpenRouter transport) takes precedence
        over ``reasoning_via_thinking_kwarg`` (direct Anthropic) when both
        are set, because the actual transport is OpenRouter — OpenRouter
        silently drops the top-level ``thinking={…}`` kwarg.
        """
        # Preset mis-configuration guard — fail loud per AGENTS.md.
        # Only applies when the direct-Anthropic transport is the active route
        # (no OpenRouter overlay); OpenRouter handles ``adaptive`` natively
        # via ``extra_body.reasoning.effort`` for thinking-kwarg presets too.
        if (
            self._reasoning_via_thinking_kwarg
            and not self._reasoning_via_extra_body
            and cfg.mode not in ("budget", "off")
        ):
            raise ValueError(
                "Preset declares reasoning_via_thinking_kwarg=True but "
                f"ReasoningConfig.mode={cfg.mode!r}; expected 'budget' or 'off'. "
                "Thinking-kwarg-native presets (e.g. anthropic_claude_4_7) require "
                "ReasoningConfig(mode='budget', budget_tokens=N)."
            )
        if cfg.mode == "off":
            return
        if cfg.mode == "budget":
            self._apply_budget_mode(kwargs, cfg)
            return
        if cfg.mode == "adaptive":
            self._emit_effort_kwargs(kwargs, cfg.effort_hint)

    def _apply_budget_mode(self, kwargs: dict[str, Any], cfg: ReasoningConfig) -> None:
        """Budget mode dispatch: extra_body (OpenRouter) > thinking kwarg
        (direct Anthropic) > effort fallback."""
        if self._reasoning_via_extra_body:
            budget = self._resolve_budget(cfg)
            self._emit_extra_body_reasoning(kwargs, {"max_tokens": budget})
            self._maybe_drop_sampling_params(kwargs)
            return
        if self._reasoning_via_thinking_kwarg:
            budget = self._resolve_budget(cfg)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            self._maybe_drop_sampling_params(kwargs)
            return
        # Plain preset (no thinking-native transport): fall back to effort
        # signalling. ``budget_tokens`` has no canonical cross-provider
        # mapping, so we only emit something if the caller supplied an
        # ``effort_hint``.
        self._emit_effort_kwargs(kwargs, cfg.effort_hint)

    def _resolve_budget(self, cfg: ReasoningConfig) -> int:
        """Concrete budget from explicit config > preset default; raise if neither."""
        budget = cfg.budget_tokens
        if budget is None:
            budget = self._reasoning_budget_default
        if budget is None:
            raise ValueError(
                "ReasoningConfig(mode='budget') requires budget_tokens; "
                "preset provided no default. Set "
                "ReasoningConfig(mode='budget', budget_tokens=N) explicitly."
            )
        return budget

    def rule_for(self, param: str, value: str | None) -> str | None:
        """The declared action for ``value`` of ``param``, or ``None``.

        Read by every site that attaches a rulable parameter — the effort path
        below, and ``LLMClient._build_kwargs`` for ``tool_choice``. Returning
        the action rather than acting keeps the decision where the parameter is
        known: only the caller can say what refusing or omitting means there.
        """
        if value is None:
            return None
        rule = self._param_value_rules.get((param, value.lower()))
        return rule.action if rule else None

    def rule_evidence(self, param: str, value: str | None) -> str | None:
        """The evidence recorded for a declared value gap, for error messages."""
        if value is None:
            return None
        rule = self._param_value_rules.get((param, value.lower()))
        return rule.evidence if rule else None

    def rule_substitute(self, param: str, value: str | None) -> str | None:
        """The replacement an ``override`` rule declares, or ``None``."""
        if value is None:
            return None
        rule = self._param_value_rules.get((param, value.lower()))
        return rule.substitute if rule else None

    def warn_substituted(self, param: str, requested: str, sent: str) -> None:
        """Log that the request no longer carries what the caller asked for.

        An ``override`` is the one action that changes the request's meaning,
        and nothing downstream can tell from the response that it happened. The
        engine cannot know what its callers do with the result, so it records
        the substitution where any of them can see it.
        """
        logger.warning(
            "param_value_rules: sent %s=%r instead of the requested %r "
            "(evidence: %s). Results from this call are not directly "
            "comparable with calls that sent %r.",
            param,
            sent,
            requested,
            self.rule_evidence(param, requested),
            requested,
        )

    def _emit_effort_kwargs(self, kwargs: dict[str, Any], effort_hint: str | None) -> None:
        """Emit provider-flavoured effort kwargs for adaptive / fallback modes."""
        if effort_hint is None:
            return
        effort = effort_hint.lower()
        action = self.rule_for("reasoning_effort", effort)
        if action == RuleAction.REJECT:
            # Both the refused set and the remaining choices come from the
            # rules, and only `reject` rules count: a `drop` or `override` on
            # another level is still usable, so listing it would tell the
            # operator a level is unavailable when it is not.
            refused = {
                v
                for (param, v), rule in self._param_value_rules.items()
                if param == "reasoning_effort" and rule.action == RuleAction.REJECT
            }
            supported = tuple(e for e in ("low", "medium", "high", "xhigh") if e not in refused)
            evidence = self.rule_evidence("reasoning_effort", effort)
            raise ValueError(
                f"ReasoningConfig(effort_hint={effort!r}) is declared "
                f"unsupported for this provider+model combination "
                f"(refused: {sorted(refused)}). Evidence: {evidence}. "
                f"Use one of {list(supported)!r}, or route through a transport "
                f"that supports this effort level (e.g. OpenRouter rather than "
                f"the direct provider, when available). See "
                f"tolokaforge_models/data/model_presets.yaml for the declarations."
            )
        if action == RuleAction.DROP:
            # Omitting the parameter yields the provider's DEFAULT budget, not
            # the level asked for, so this is a real change to the request. The
            # caller declared it; log it and move on.
            self.warn_substituted("reasoning_effort", effort, "<omitted>")
            return
        if action == "override":
            substitute = self.rule_substitute("reasoning_effort", effort)
            if substitute:
                self.warn_substituted("reasoning_effort", effort, substitute)
                effort = substitute
        if self._reasoning_via_extra_body:
            self._emit_extra_body_reasoning(kwargs, {"effort": effort})
        else:
            kwargs["reasoning_effort"] = effort

    @staticmethod
    def _emit_extra_body_reasoning(
        kwargs: dict[str, Any], reasoning_fields: dict[str, Any]
    ) -> None:
        """Merge a ``{...}`` block into ``kwargs.extra_body.reasoning``.

        Always sets ``enabled: True`` so OpenRouter activates reasoning for
        the request; the caller-supplied fields (``effort`` / ``max_tokens``)
        compose with that flag.
        """
        extra_body = kwargs.get("extra_body", {})
        extra_body["reasoning"] = {**reasoning_fields, "enabled": True}
        kwargs["extra_body"] = extra_body

    def _maybe_drop_sampling_params(self, kwargs: dict[str, Any]) -> None:
        """Drop ``temperature`` / ``top_p`` / ``top_k`` when thinking is active.

        Anthropic's API ignores these when thinking is enabled and raises 400
        on raw-API calls with non-default values; OpenRouter silently strips
        them today. Surfacing the drop on our side makes the request shape
        explicit and future-proofs against provider tightening.
        """
        if not self._drop_sampling_when_thinking:
            return
        for key in _SAMPLING_KEYS:
            kwargs.pop(key, None)
