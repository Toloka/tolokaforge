"""Generation-parameter adaptation for a target model.

:class:`GenerationParams` is the :class:`ParamPolicy` implementation shipped
today — it composes temperature / seed / reasoning kwargs per-model.

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

from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.llm.reasoning import ReasoningConfig

__all__ = [
    "ParamPolicy",
    "GenerationParams",
]

_SAMPLING_KEYS: tuple[str, ...] = ("temperature", "top_p", "top_k")


@runtime_checkable
class ParamPolicy(Protocol):
    """Adapts generation parameters for the target model."""

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


class GenerationParams:
    """Adapts generation kwargs based on model constraints.

    Configurable once at construction; ``adapt()`` applies the rules on every
    ``generate()`` call.
    """

    def __init__(
        self,
        fixed_temperature: float | None = None,
        supports_seed: bool = True,
        reasoning_via_extra_body: bool = False,
        reasoning_via_thinking_kwarg: bool = False,
        drop_sampling_when_thinking: bool = False,
        reasoning_budget_default: int | None = None,
        unsupported_effort_levels: frozenset[str] | list[str] | tuple[str, ...] | None = None,
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

    def _emit_effort_kwargs(self, kwargs: dict[str, Any], effort_hint: str | None) -> None:
        """Emit provider-flavoured effort kwargs for adaptive / fallback modes."""
        if effort_hint is None:
            return
        effort = effort_hint.lower()
        if effort in self._unsupported_effort_levels:
            supported = (
                ("low", "medium", "high", "xhigh")
                if not self._unsupported_effort_levels
                else tuple(
                    e
                    for e in ("low", "medium", "high", "xhigh")
                    if e not in self._unsupported_effort_levels
                )
            )
            raise ValueError(
                f"ReasoningConfig(effort_hint={effort!r}) is declared "
                f"unsupported for this provider+model combination "
                f"(unsupported_effort_levels={sorted(self._unsupported_effort_levels)}). "
                f"Use one of {list(supported)!r}, or route through a transport "
                f"that supports this effort level (e.g. OpenRouter rather than "
                f"the direct provider, when available). See "
                f"tolokaforge/core/data/model_presets.yaml for the declarations."
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
