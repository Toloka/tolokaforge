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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from tolokaforge.core.llm.reasoning import ReasoningConfig

__all__ = [
    "ParamsPolicy",
    "ParamPolicy",  # noqa: F822 — resolved via module-level __getattr__ shim
    "GenerationParams",
]

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

    action: str
    evidence: str


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
        if param not in SUPPORTED_ACTIONS:
            raise ValueError(
                f"param_value_rules: {param!r} is not a rulable parameter "
                f"(known: {sorted(SUPPORTED_ACTIONS)}). A rule on a parameter "
                f"the engine never sends would silently do nothing."
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
            action = str(spec.get("action", "")).lower()
            # Per-parameter, not global: an action is legal only where a consult
            # site implements it. A globally-valid action would type-check and
            # then do nothing wherever nobody wired it up.
            if action not in SUPPORTED_ACTIONS[param]:
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: action "
                    f"{spec.get('action')!r} is not implemented for {param!r} "
                    f"(supported: {sorted(SUPPORTED_ACTIONS[param])}). An action "
                    f"with no consult site would be accepted here and then "
                    f"silently ignored on the wire."
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
            if action == "drop" and OMISSION_EQUIVALENT_VALUE.get(param) != normalised_value:
                equivalent = OMISSION_EQUIVALENT_VALUE.get(param)
                detail = (
                    f"only {equivalent!r} may be dropped for {param!r}"
                    if equivalent
                    else f"no value of {param!r} may be dropped"
                )
                raise ValueError(
                    f"param_value_rules[{param!r}][{value!r}]: action 'drop' is "
                    f"refused because omitting {param!r} is not documented as "
                    f"equivalent to {value!r} ({detail}). Dropping it would "
                    f"change what was asked without saying so; use 'reject'."
                )
            rules[(param, normalised_value)] = _ValueRule(action=action, evidence=evidence)
    return rules


#: The declared contract, as ONE table: which parameters may carry a rule, and
#: which actions are actually implemented for each. Three separate constants
#: (rulable params, valid actions, drop-legality) could describe a cell nobody
#: had wired up — ``tool_choice: reject`` type-checked, constructed cleanly and
#: then did nothing, because the client only ever tested for ``drop``. A single
#: table cannot express an unimplemented cell.
#:
#: An action appears here only when a consult site exists:
#:
#: * ``reasoning_effort: reject`` — ``GenerationParams._emit_effort_kwargs``
#:   raises before the request is built. ``drop`` is absent on purpose: omitting
#:   the parameter yields the provider's default budget, not the level asked
#:   for, so dropping it would change the measurement without saying so.
#: * ``tool_choice: drop`` — ``LLMClient._build_kwargs`` omits the parameter.
#:   Legal because omission is how the OpenAI-shaped envelope says "the model
#:   decides", which is what ``auto`` names; Cohere's Chat API has no ``AUTO``
#:   at all and documents omission as its equivalent
#:   (https://docs.cohere.com/reference/chat). ``reject`` is absent because no
#:   site raises on it — add the site first, then the cell.
#:
#: Adding a parameter means adding a consult site wherever it is attached, so
#: this table stays honest by construction: an entry without a site is a cell
#: that silently does nothing, which is what it exists to prevent.
SUPPORTED_ACTIONS: Final[dict[str, frozenset[str]]] = {
    "reasoning_effort": frozenset({"reject"}),
    "tool_choice": frozenset({"drop"}),
}

#: Values whose omission the provider documents as equivalent to sending them.
#: Consulted only for ``drop``: dropping any other value changes the request.
OMISSION_EQUIVALENT_VALUE: Final[dict[str, str]] = {"tool_choice": "auto"}


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
            "unsupported_effort_levels",
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
        unsupported_effort_levels: frozenset[str] | list[str] | tuple[str, ...] | None = None,
        param_value_rules: dict[str, dict[str, dict[str, str]]] | None = None,
    ):
        self._fixed_temperature = fixed_temperature
        self._supports_seed = supports_seed
        self._reasoning_via_extra_body = reasoning_via_extra_body
        self._reasoning_via_thinking_kwarg = reasoning_via_thinking_kwarg
        self._drop_sampling_when_thinking = drop_sampling_when_thinking
        self._reasoning_budget_default = reasoning_budget_default
        # Per-provider known-broken effort levels (AGENTS.md rule #1: surface
        # failures explicitly rather than silently mapping). Populated by
        # presets / provider overlays when an effort level is known to break
        # upstream — e.g. litellm's direct ``gemini/*`` path silently returns
        # empty responses for Gemini 3.1 Pro when ``reasoning_effort='medium'``
        # is combined with tool_calls (verified 2026-05-21,
        # BerriAI/litellm#19403-class). ``_emit_effort_kwargs`` raises
        # ``ValueError`` rather than mapping to a working level — the caller
        # picks the workaround.
        # Normalise YAML lists into a frozenset so equality + membership are
        # cheap and ``GenerationParams`` is still hashable-friendly.
        self._unsupported_effort_levels: frozenset[str] = frozenset(
            e.lower() for e in (unsupported_effort_levels or ())
        )
        # ``param_value_rules`` is the general form of the line above: "this
        # provider or model will not take value V of parameter P, and here is
        # what to do about it". ``unsupported_effort_levels`` is the same
        # statement for one parameter, kept working and folded in below so
        # shipped presets and operator overlays do not have to move at once.
        self._param_value_rules: dict[tuple[str, str], _ValueRule] = _normalise_value_rules(
            param_value_rules
        )
        for level in self._unsupported_effort_levels:
            key = ("reasoning_effort", level)
            self._param_value_rules.setdefault(
                key,
                _ValueRule(
                    action="reject",
                    evidence="declared via the unsupported_effort_levels shorthand",
                ),
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

    def _emit_effort_kwargs(self, kwargs: dict[str, Any], effort_hint: str | None) -> None:
        """Emit provider-flavoured effort kwargs for adaptive / fallback modes."""
        if effort_hint is None:
            return
        effort = effort_hint.lower()
        if self.rule_for("reasoning_effort", effort) == "reject":
            # Derive both the refused set and the remaining choices from the
            # rules, not from the legacy field: a rejection declared through
            # `param_value_rules` would otherwise report an empty
            # `unsupported_effort_levels` and read as a bug in the engine.
            refused = {v for (param, v) in self._param_value_rules if param == "reasoning_effort"}
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
