"""``param_value_rules`` — the general form of "this route will not take that value".

Two shipped cases motivate it and they sit at different layers, which is the
point of the mechanism: the same declaration works wherever a ``params:`` block
is legal.

* **Provider layer** — the direct ``gemini`` transport refuses
  ``reasoning_effort='medium'`` because of a litellm defect. Route-specific and
  temporary; the OpenRouter route is unaffected.
* **Model layer** — Cohere's Chat API has no ``AUTO`` for ``tool_choice`` at
  all. Vendor contract, permanent, true on every route.

Before this, each such gap cost a bespoke constructor kwarg plus a use site,
i.e. an engine release per gap. These lock the general form so the next one is
a YAML line.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.llm.reasoning import ReasoningConfig

pytestmark = pytest.mark.unit


_COHERE_RULES = {
    "tool_choice": {
        "auto": {
            "action": "drop",
            "evidence": "2026-08-12, Cohere Chat API: no AUTO; omission is its documented equal",
        }
    }
}


def _rules(param: str, value: str, action: str, evidence: str = "evidence") -> dict:
    return {param: {value: {"action": action, "evidence": evidence}}}


class TestDeclaration:
    def test_drop_is_reported_for_the_declared_value_only(self) -> None:
        policy = GenerationParams(param_value_rules=_COHERE_RULES)
        assert policy.rule_for("tool_choice", "auto") == "drop"
        # REQUIRED / NONE are values Cohere honours; a blanket "no tool_choice"
        # would suppress them and change what the caller asked for.
        assert policy.rule_for("tool_choice", "required") is None
        assert policy.rule_for("tool_choice", "none") is None

    def test_lookup_is_case_insensitive_and_none_safe(self) -> None:
        policy = GenerationParams(param_value_rules=_COHERE_RULES)
        assert policy.rule_for("tool_choice", "AUTO") == "drop"
        assert policy.rule_for("tool_choice", None) is None

    def test_evidence_is_retrievable_for_error_messages(self) -> None:
        policy = GenerationParams(param_value_rules=_COHERE_RULES)
        assert "Cohere" in (policy.rule_evidence("tool_choice", "auto") or "")

    def test_a_policy_without_rules_answers_none(self) -> None:
        assert GenerationParams().rule_for("tool_choice", "auto") is None


class TestGuards:
    """Each guard exists because the alternative is a preset that looks like it
    does something and quietly does not."""

    def test_unknown_parameter_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a rulable parameter"):
            GenerationParams(param_value_rules=_rules("top_p", "0", "reject"))

    def test_unknown_action_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not implemented"):
            GenerationParams(param_value_rules=_rules("tool_choice", "auto", "override"))

    def test_an_action_with_no_consult_site_is_refused(self) -> None:
        # `reject` is a real action, and `tool_choice` is a real parameter, but
        # nothing raises on tool_choice — the client only ever tests for `drop`.
        # Accepting this cell would construct cleanly and then do nothing.
        with pytest.raises(ValueError, match="not implemented for 'tool_choice'"):
            GenerationParams(param_value_rules=_rules("tool_choice", "auto", "reject"))

    def test_every_declared_cell_has_a_consult_site(self) -> None:
        # The contract table is the whole guard against defect-by-omission, so
        # assert its shape rather than trusting it to stay curated.
        from tolokaforge.core.llm.params_policy import (
            OMISSION_EQUIVALENT_VALUE,
            SUPPORTED_ACTIONS,
        )

        assert {
            "reasoning_effort": frozenset({"reject"}),
            "tool_choice": frozenset({"drop"}),
        } == SUPPORTED_ACTIONS
        # Anything that may be dropped must have a documented omission-equal.
        for param, actions in SUPPORTED_ACTIONS.items():
            if "drop" in actions:
                assert (
                    param in OMISSION_EQUIVALENT_VALUE
                ), f"{param} allows drop but declares no omission-equivalent value"

    def test_evidence_is_required(self) -> None:
        with pytest.raises(ValueError, match="'evidence' is required"):
            GenerationParams(param_value_rules={"tool_choice": {"auto": {"action": "drop"}}})

    def test_blank_evidence_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'evidence' is required"):
            GenerationParams(param_value_rules=_rules("tool_choice", "auto", "drop", "   "))

    def test_drop_is_refused_where_omission_changes_the_request(self) -> None:
        # Omitting reasoning_effort yields the provider's default budget, not
        # the level that was asked for. Only `reject` is honest there, and the
        # contract table refuses the cell before the value check is reached.
        with pytest.raises(ValueError, match="not implemented for 'reasoning_effort'"):
            GenerationParams(param_value_rules=_rules("reasoning_effort", "medium", "drop"))

    def test_a_non_mapping_block_is_refused_with_context(self) -> None:
        with pytest.raises(ValueError, match="expected a mapping of parameter"):
            GenerationParams(param_value_rules=["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_drop_is_refused_for_a_value_omission_does_not_name(self) -> None:
        with pytest.raises(ValueError, match="not documented as equivalent"):
            GenerationParams(param_value_rules=_rules("tool_choice", "required", "drop"))


class TestRejectPath:
    def test_reject_raises_and_names_the_remaining_choices(self) -> None:
        policy = GenerationParams(
            param_value_rules=_rules("reasoning_effort", "medium", "reject", "litellm#19403")
        )
        kwargs: dict = {}
        with pytest.raises(ValueError) as excinfo:
            policy.adapt(
                kwargs,
                config_temperature=None,
                config_seed=None,
                config_reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
                temperature=None,
                seed=None,
                reasoning=None,
            )
        message = str(excinfo.value)
        assert "litellm#19403" in message, "the evidence must reach the operator, not just the log"
        assert "'low'" in message and "'high'" in message
        assert "'medium'" not in message.split("Use one of")[1]

    def test_a_permitted_level_still_emits(self) -> None:
        policy = GenerationParams(param_value_rules=_rules("reasoning_effort", "medium", "reject"))
        kwargs: dict = {}
        policy.adapt(
            kwargs,
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(mode="adaptive", effort_hint="high"),
            temperature=None,
            seed=None,
            reasoning=None,
        )
        assert kwargs["reasoning_effort"] == "high"


class TestLegacyShorthand:
    """``unsupported_effort_levels`` keeps working — shipped overlays use it."""

    def test_shorthand_folds_into_rules(self) -> None:
        policy = GenerationParams(unsupported_effort_levels=["medium"])
        assert policy.rule_for("reasoning_effort", "medium") == "reject"
        assert policy.rule_for("reasoning_effort", "high") is None

    def test_explicit_rule_wins_over_the_shorthand(self) -> None:
        policy = GenerationParams(
            unsupported_effort_levels=["medium"],
            param_value_rules=_rules("reasoning_effort", "medium", "reject", "the real evidence"),
        )
        assert policy.rule_evidence("reasoning_effort", "medium") == "the real evidence"


class TestShippedData:
    """The migrated Gemini declaration must behave exactly as before the move."""

    def test_direct_gemini_route_still_refuses_medium(self) -> None:
        policy = build_capabilities("google/gemini-3.1-pro", provider="gemini").params_policy
        assert policy.rule_for("reasoning_effort", "medium") == "reject"

    def test_openrouter_route_is_untouched(self) -> None:
        # The comment on the declaration has always said the OpenRouter route
        # is unaffected; this is that claim as a test rather than prose.
        policy = build_capabilities("google/gemini-3.1-pro", provider="openrouter").params_policy
        assert policy.rule_for("reasoning_effort", "medium") is None

    def test_other_effort_levels_survive_on_the_direct_route(self) -> None:
        policy = build_capabilities("google/gemini-3.1-pro", provider="gemini").params_policy
        assert policy.rule_for("reasoning_effort", "high") is None
        assert policy.rule_for("reasoning_effort", "low") is None


class TestClientWiring:
    """`rule_for` returning "drop" is worth nothing unless the client acts on it.

    The gap these cover is how `tool_choice: reject` survived review as a cell
    that constructed cleanly and then did nothing on the wire.
    """

    @staticmethod
    def _kwargs_for(rules: dict | None) -> dict:
        from tolokaforge.core.llm.capabilities import ModelCapabilities

        caps = ModelCapabilities(params_policy=GenerationParams(param_value_rules=rules))
        kwargs: dict = {}
        tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
        # Mirror the client's attach rule rather than standing up a client:
        # tool_choice is only ever set inside `if tools:`.
        if tools:
            kwargs["tools"] = tools
            dropped = caps.params_policy.rule_for("tool_choice", "auto") == "drop"
            if not dropped:
                kwargs["tool_choice"] = "auto"
        return kwargs

    def test_without_a_rule_tool_choice_is_sent(self) -> None:
        assert self._kwargs_for(None)["tool_choice"] == "auto"

    def test_a_drop_rule_removes_it_from_the_request(self) -> None:
        assert "tool_choice" not in self._kwargs_for(_COHERE_RULES)

    def test_the_client_reads_the_policy_rather_than_a_bespoke_flag(self) -> None:
        # Locks the wiring itself: a refactor that reintroduces a per-model
        # boolean here would pass every other test in this file.
        from pathlib import Path

        source = Path("tolokaforge/core/llm/client.py").read_text(encoding="utf-8")
        assert 'rule_for("tool_choice", tool_choice)' in source


class TestLayering:
    """Rules merge per parameter and per value across layers.

    A shallow merge here silently disarms a guard nobody touched: declaring a
    `tool_choice` rule in an overlay would drop the bundled `reasoning_effort`
    rule with it, and the eval would then run with the quirk unguarded.
    """

    @staticmethod
    def _with_overlay(overlay: dict):
        import tempfile
        from pathlib import Path

        import yaml

        from tolokaforge.core.llm import presets

        path = Path(tempfile.mkdtemp()) / "overlay.yaml"
        path.write_text(yaml.dump(overlay), encoding="utf-8")
        presets.set_overlay_path(str(path))
        try:
            return build_capabilities("google/gemini-3.1-pro", provider="gemini").params_policy
        finally:
            presets.set_overlay_path(None)

    def test_an_overlay_rule_does_not_delete_the_bundled_one(self) -> None:
        policy = self._with_overlay(
            {
                "providers": {
                    "gemini": {
                        "params": {
                            "param_value_rules": {
                                "tool_choice": {"auto": {"action": "drop", "evidence": "overlay"}}
                            }
                        }
                    }
                }
            }
        )
        assert policy.rule_for("tool_choice", "auto") == "drop", "the overlay rule must apply"
        assert (
            policy.rule_for("reasoning_effort", "medium") == "reject"
        ), "the bundled gemini guard must survive an unrelated overlay rule"

    def test_an_overlay_can_still_override_the_same_declaration(self) -> None:
        policy = self._with_overlay(
            {
                "providers": {
                    "gemini": {
                        "params": {
                            "param_value_rules": {
                                "reasoning_effort": {
                                    "medium": {"action": "reject", "evidence": "operator says so"}
                                }
                            }
                        }
                    }
                }
            }
        )
        assert policy.rule_evidence("reasoning_effort", "medium") == "operator says so"
