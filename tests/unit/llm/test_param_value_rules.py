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

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm import presets
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.llm.reasoning import ReasoningConfig
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


@contextmanager
def _overlay(tmp_path: Path, overlay: dict[str, Any]):
    """Install ``overlay`` for the block. Shared by both suites below, on
    pytest's ``tmp_path`` so nothing survives the run."""
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.dump(overlay), encoding="utf-8")
    presets.set_overlay_path(str(path))
    try:
        yield
    finally:
        presets.set_overlay_path(None)


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
        with pytest.raises(ValueError, match="is not one of"):
            GenerationParams(param_value_rules=_rules("tool_choice", "auto", "coerce"))

    def test_evidence_is_required(self) -> None:
        with pytest.raises(ValueError, match="'evidence' is required"):
            GenerationParams(param_value_rules={"tool_choice": {"auto": {"action": "drop"}}})

    def test_blank_evidence_is_refused(self) -> None:
        with pytest.raises(ValueError, match="'evidence' is required"):
            GenerationParams(param_value_rules=_rules("tool_choice", "auto", "drop", "   "))

    def test_an_unknown_key_in_a_rule_is_refused(self) -> None:
        # `until:`, `note:`, `owner:` — plausible things to write, none of them
        # read by anything. Accepting one would be the same silent no-op every
        # other guard in this function exists to stop.
        with pytest.raises(ValueError, match=r"unknown key\(s\) \['until'\]"):
            GenerationParams(
                param_value_rules={
                    "tool_choice": {"auto": {"action": "drop", "evidence": "e", "until": "2027-01"}}
                }
            )

    def test_the_unknown_key_error_names_the_legal_ones(self) -> None:
        with pytest.raises(ValueError, match="'action', 'evidence', 'with'"):
            GenerationParams(
                param_value_rules={
                    "tool_choice": {"auto": {"action": "drop", "evidence": "e", "note": "x"}}
                }
            )

    def test_a_non_mapping_block_is_refused_with_context(self) -> None:
        with pytest.raises(ValueError, match="expected a mapping of parameter"):
            GenerationParams(param_value_rules=["not", "a", "mapping"])  # type: ignore[arg-type]


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


class TestLayering:
    """Rules merge per parameter and per value across layers.

    A shallow merge here silently disarms a guard nobody touched: declaring a
    `tool_choice` rule in an overlay would drop the bundled `reasoning_effort`
    rule with it, and the eval would then run with the quirk unguarded.
    """

    @staticmethod
    def _with_overlay(overlay: dict, tmp_path: Path):
        with _overlay(tmp_path, overlay):
            return build_capabilities("google/gemini-3.1-pro", provider="gemini").params_policy

    def test_an_overlay_rule_does_not_delete_the_bundled_one(self, tmp_path: Path) -> None:
        policy = self._with_overlay(
            tmp_path=tmp_path,
            overlay={
                "providers": {
                    "gemini": {
                        "params": {
                            "param_value_rules": {
                                "tool_choice": {"auto": {"action": "drop", "evidence": "overlay"}}
                            }
                        }
                    }
                }
            },
        )
        assert policy.rule_for("tool_choice", "auto") == "drop", "the overlay rule must apply"
        assert (
            policy.rule_for("reasoning_effort", "medium") == "reject"
        ), "the bundled gemini guard must survive an unrelated overlay rule"

    def test_an_overlay_can_still_override_the_same_declaration(self, tmp_path: Path) -> None:
        policy = self._with_overlay(
            tmp_path=tmp_path,
            overlay={
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
            },
        )
        assert policy.rule_evidence("reasoning_effort", "medium") == "operator says so"


class TestOverride:
    """`override` sends something other than what was asked.

    It is the one action that changes the meaning of the request, so the tests
    here are as much about the guard rails as the behaviour.
    """

    def test_the_substitute_is_sent_instead(self) -> None:
        policy = GenerationParams(
            param_value_rules={
                "reasoning_effort": {
                    "medium": {"action": "override", "with": "low", "evidence": "litellm#19403"}
                }
            }
        )
        kwargs: dict = {}
        policy.adapt(
            kwargs,
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
            temperature=None,
            seed=None,
            reasoning=None,
        )
        assert kwargs["reasoning_effort"] == "low"

    def test_every_substitution_is_logged_with_both_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Nothing in the response says a substitution happened, so the log line
        # is the only trace a caller has. It must name what was asked for as
        # well as what was sent, or it cannot be acted on.
        policy = GenerationParams(
            param_value_rules={
                "reasoning_effort": {
                    "medium": {"action": "override", "with": "low", "evidence": "why"}
                }
            }
        )
        with caplog.at_level("WARNING"):
            policy.adapt(
                {},
                config_temperature=None,
                config_seed=None,
                config_reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
                temperature=None,
                seed=None,
                reasoning=None,
            )
        assert "'medium'" in caplog.text and "'low'" in caplog.text
        assert "why" in caplog.text
        assert "not directly comparable" in caplog.text

    def test_override_requires_a_replacement(self) -> None:
        with pytest.raises(ValueError, match="requires a 'with' value"):
            GenerationParams(
                param_value_rules={
                    "reasoning_effort": {"medium": {"action": "override", "evidence": "x"}}
                }
            )

    def test_a_no_op_override_is_refused(self) -> None:
        with pytest.raises(ValueError, match="that rule does nothing"):
            GenerationParams(
                param_value_rules={
                    "reasoning_effort": {
                        "medium": {"action": "override", "with": "medium", "evidence": "x"}
                    }
                }
            )

    def test_substituting_into_another_declared_gap_is_refused(self) -> None:
        # Overriding medium -> low when low is itself declared unusable would
        # send a value the same block calls broken.
        with pytest.raises(ValueError, match="also declares a rule for"):
            GenerationParams(
                param_value_rules={
                    "reasoning_effort": {
                        "medium": {"action": "override", "with": "low", "evidence": "x"},
                        "low": {"action": "reject", "evidence": "y"},
                    }
                }
            )

    def test_with_is_meaningless_on_other_actions(self) -> None:
        with pytest.raises(ValueError, match="only meaningful for action 'override'"):
            GenerationParams(
                param_value_rules={
                    "reasoning_effort": {
                        "medium": {"action": "reject", "with": "low", "evidence": "x"}
                    }
                }
            )


class TestThroughTheRealClient:
    """Drive ``LLMClient._build_kwargs`` itself.

    An earlier revision re-implemented the client's branch logic inside the
    test and asserted on the copy. That cannot prove the block sits inside
    ``if tools:``, runs before the attach, or is reached at all — which is the
    exact failure mode this file exists to catch.
    """

    @staticmethod
    def _kwargs(
        rules: dict | None,
        tmp_path: Path,
        tool_choice: str | None = "auto",
        tools: bool = True,
    ) -> dict:
        overlay = {"providers": {"mock": {"params": {"param_value_rules": rules or {}}}}}
        with _overlay(tmp_path, overlay):
            client = LLMClient(ModelConfig(provider="mock", name="mock-model"))
            return client._build_kwargs(
                system=None,
                messages=[],
                tools=(
                    [{"type": "function", "function": {"name": "n", "parameters": {}}}]
                    if tools
                    else None
                ),
                tool_choice=tool_choice,
                temperature=None,
                seed=None,
                reasoning=None,
                top_p=None,
                max_tokens=None,
            )

    def test_no_rule_sends_tool_choice(self, tmp_path: Path) -> None:
        assert self._kwargs(tmp_path=tmp_path, rules=None)["tool_choice"] == "auto"

    def test_drop_removes_it_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            kwargs = self._kwargs(
                tmp_path=tmp_path, rules=_rules("tool_choice", "auto", "drop", "vendor has no AUTO")
            )
        assert "tool_choice" not in kwargs
        assert "vendor has no AUTO" in caplog.text

    def test_override_substitutes_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            kwargs = self._kwargs(
                tmp_path=tmp_path,
                rules={
                    "tool_choice": {
                        "auto": {"action": "override", "with": "required", "evidence": "e"}
                    }
                },
            )
        assert kwargs["tool_choice"] == "required"
        assert "not directly comparable" in caplog.text

    def test_reject_raises_naming_the_evidence(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="declared unusable"):
            self._kwargs(
                tmp_path=tmp_path, rules=_rules("tool_choice", "auto", "reject", "the reason")
            )

    def test_an_unruled_value_is_untouched(self, tmp_path: Path) -> None:
        # Only the declared value is affected; `required` passes through.
        kwargs = self._kwargs(
            tmp_path=tmp_path,
            rules=_rules("tool_choice", "auto", "drop", "e"),
            tool_choice="required",
        )
        assert kwargs["tool_choice"] == "required"

    def test_rules_are_inert_without_tools(self, tmp_path: Path) -> None:
        # tool_choice is only ever attached alongside tools, so a rule cannot
        # fire on a toolless call. Documented here rather than left surprising.
        kwargs = self._kwargs(
            tmp_path=tmp_path, rules=_rules("tool_choice", "auto", "reject", "e"), tools=False
        )
        assert "tool_choice" not in kwargs


class TestEffortDrop:
    """`reasoning_effort: drop` — the sixth cell of the 2x3 matrix.

    Both halves matter and neither is implied by the other: the emission has to
    be suppressed on *both* transports, and the substitution has to be logged.
    Remove the early `return` and the dropped level ships silently, which is
    the failure this whole mechanism exists to prevent.
    """

    @staticmethod
    def _adapt(policy: GenerationParams) -> dict:
        kwargs: dict = {}
        policy.adapt(
            kwargs,
            config_temperature=None,
            config_seed=None,
            config_reasoning=ReasoningConfig(mode="adaptive", effort_hint="medium"),
            temperature=None,
            seed=None,
            reasoning=None,
        )
        return kwargs

    def test_nothing_is_emitted_on_the_plain_transport(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        policy = GenerationParams(
            param_value_rules=_rules("reasoning_effort", "medium", "drop", "provider chokes on it")
        )
        with caplog.at_level("WARNING"):
            kwargs = self._adapt(policy)
        assert "reasoning_effort" not in kwargs
        assert "extra_body" not in kwargs
        assert "provider chokes on it" in caplog.text
        assert "not directly comparable" in caplog.text

    def test_nothing_is_emitted_on_the_extra_body_transport(self) -> None:
        # The OpenRouter path emits through extra_body rather than a top-level
        # kwarg, so a drop that only guarded one branch would leak here.
        policy = GenerationParams(
            reasoning_via_extra_body=True,
            param_value_rules=_rules("reasoning_effort", "medium", "drop", "e"),
        )
        kwargs = self._adapt(policy)
        assert "extra_body" not in kwargs
        assert "reasoning_effort" not in kwargs

    def test_an_unruled_level_still_emits(self) -> None:
        policy = GenerationParams(
            param_value_rules=_rules("reasoning_effort", "medium", "drop", "e")
        )
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
