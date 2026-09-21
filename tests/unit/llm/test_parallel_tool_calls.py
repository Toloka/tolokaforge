"""``parallel_tool_calls`` is a per-request parameter the engine sends
alongside ``tools`` when a :class:`ModelConfig` sets it. Some analysis-agent
workflows depend on serial tool calls — one tool per model turn — and set
``parallel_tool_calls=False``; the wire admits the parameter via
``supports_function_calling`` and rules on it are declarable through
``param_value_rules``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm import presets
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.litellm_params import FLAG_PARAMS
from tolokaforge.core.llm.params_policy import RULABLE_PARAMS
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


@contextmanager
def _overlay(tmp_path: Path, overlay: dict[str, Any]):
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.dump(overlay), encoding="utf-8")
    presets.set_overlay_path(str(path))
    try:
        yield
    finally:
        presets.set_overlay_path(None)


def _rules(param: str, value: str, action: str, evidence: str = "e", substitute: str | None = None):
    spec = {"action": action, "evidence": evidence}
    if substitute is not None:
        spec["with"] = substitute
    return {param: {value: spec}}


class TestAllowSet:
    def test_parallel_tool_calls_is_in_the_function_calling_flag_admit_list(self):
        assert "parallel_tool_calls" in FLAG_PARAMS["supports_function_calling"]

    def test_parallel_tool_calls_is_rulable(self):
        assert "parallel_tool_calls" in RULABLE_PARAMS


class TestModelConfig:
    def test_default_is_none(self):
        assert ModelConfig(provider="mock", name="m").parallel_tool_calls is None

    def test_accepts_true_and_false(self):
        assert (
            ModelConfig(provider="mock", name="m", parallel_tool_calls=False).parallel_tool_calls
            is False
        )
        assert (
            ModelConfig(provider="mock", name="m", parallel_tool_calls=True).parallel_tool_calls
            is True
        )


class TestConsultSite:
    """Drive ``LLMClient._build_kwargs`` — proves the parameter reaches
    the request kwargs alongside ``tools``, and that ``param_value_rules``
    over it fire from the same site the ``tool_choice`` rules do.
    """

    @staticmethod
    def _kwargs(
        tmp_path: Path,
        *,
        parallel_tool_calls: bool | None,
        rules: dict | None = None,
        tools: bool = True,
    ) -> dict:
        overlay = {"providers": {"mock": {"params": {"param_value_rules": rules or {}}}}}
        with _overlay(tmp_path, overlay):
            client = LLMClient(
                ModelConfig(
                    provider="mock", name="mock-model", parallel_tool_calls=parallel_tool_calls
                )
            )
            return client._build_kwargs(
                system=None,
                messages=[],
                tools=(
                    [{"type": "function", "function": {"name": "n", "parameters": {}}}]
                    if tools
                    else None
                ),
                tool_choice="auto",
                temperature=None,
                seed=None,
                reasoning=None,
                top_p=None,
                max_tokens=None,
            )

    def test_default_none_omits_the_parameter(self, tmp_path: Path) -> None:
        kwargs = self._kwargs(tmp_path, parallel_tool_calls=None)
        assert "parallel_tool_calls" not in kwargs

    def test_false_sends_false(self, tmp_path: Path) -> None:
        kwargs = self._kwargs(tmp_path, parallel_tool_calls=False)
        assert kwargs["parallel_tool_calls"] is False

    def test_true_sends_true(self, tmp_path: Path) -> None:
        kwargs = self._kwargs(tmp_path, parallel_tool_calls=True)
        assert kwargs["parallel_tool_calls"] is True

    def test_never_sent_without_tools(self, tmp_path: Path) -> None:
        kwargs = self._kwargs(tmp_path, parallel_tool_calls=False, tools=False)
        assert "parallel_tool_calls" not in kwargs

    def test_drop_rule_omits_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            kwargs = self._kwargs(
                tmp_path,
                parallel_tool_calls=False,
                rules=_rules("parallel_tool_calls", "false", "drop", "vendor ignores this"),
            )
        assert "parallel_tool_calls" not in kwargs
        assert "vendor ignores this" in caplog.text

    def test_reject_rule_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="declared unusable"):
            self._kwargs(
                tmp_path,
                parallel_tool_calls=False,
                rules=_rules(
                    "parallel_tool_calls", "false", "reject", "vendor refuses serial-only"
                ),
            )

    def test_override_rule_substitutes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            kwargs = self._kwargs(
                tmp_path,
                parallel_tool_calls=False,
                rules=_rules(
                    "parallel_tool_calls", "false", "override", "vendor forces parallel", "true"
                ),
            )
        assert kwargs["parallel_tool_calls"] is True
        assert "not directly comparable" in caplog.text


class TestOverlayValidation:
    """The overlay currently rejects ``param_value_rules`` for parameters not
    in ``RULABLE_PARAMS`` — with ``parallel_tool_calls`` now in the set, a
    validated overlay writes cleanly.
    """

    def test_param_value_rule_on_parallel_tool_calls_validates(self, tmp_path: Path) -> None:
        overlay = {
            "providers": {
                "mock": {
                    "params": {
                        "param_value_rules": _rules(
                            "parallel_tool_calls",
                            "false",
                            "reject",
                            "some vendor refuses serial-only",
                        )
                    }
                }
            }
        }
        with _overlay(tmp_path, overlay):
            # Construction alone triggers overlay validation via preset build.
            LLMClient(ModelConfig(provider="mock", name="mock-model"))
