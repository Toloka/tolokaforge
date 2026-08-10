"""A model missing from litellm's map is refused its parameters before sending."""

from __future__ import annotations

from pathlib import Path

import litellm
import pytest
import yaml

from tolokaforge.core.llm.litellm_params import (
    DECLARABLE_FLAGS,
    FLAG_PARAMS,
    allowed_openai_params,
)
from tolokaforge.core.llm.presets import litellm_model_entries, set_overlay_path

pytestmark = pytest.mark.unit

TOOLS = [
    {
        "type": "function",
        "function": {"name": "f", "parameters": {"type": "object", "properties": {}}},
    }
]

#: An id litellm will never carry, so the contract tests below fail only for
#: the reason they name. Keying them on a real model made them go red the day
#: upstream shipped it - reporting a broken contract when the good thing
#: happened. The gating is identical for any id the map lacks.
MID = "meta/tolokaforge-canary-not-a-real-model"
NAME = "tolokaforge-canary-not-a-real-model"
EVIDENCE = "2026-08-10, litellm 1.96.0: no entry, so meta refused tools before sending"


@pytest.fixture()
def overlay(tmp_path: Path):
    """Install a `litellm_models:` overlay, the way an operator would."""

    def _install(entries: dict) -> Path:
        path = tmp_path / "overlay.yaml"
        path.write_text(yaml.safe_dump({"litellm_models": entries}))
        set_overlay_path(str(path))
        return path

    yield _install
    set_overlay_path(None)


def _entry(**overrides) -> dict:
    return {"supports_function_calling": True, "evidence": EVIDENCE, **overrides}


def _params(allowed: list[str] | None = None, **kwargs) -> list[str] | None:
    """What litellm would put on the wire, or None when it refuses the call."""
    extra = {"allowed_openai_params": allowed} if allowed else {}
    try:
        return sorted(
            litellm.utils.get_optional_params(
                model=NAME,
                custom_llm_provider="meta",
                tools=TOOLS,
                tool_choice="auto",
                **extra,
                **kwargs,
            )
        )
    except litellm.UnsupportedParamsError:
        return None


class TestTheCanonicalContract:
    """What the installed litellm must keep doing for this design to hold.

    The premise of the whole feature is that litellm patch releases change
    parameter gating, so the escape hatch it offers is pinned here rather than
    assumed. Measured across 1.83.14 / 1.93.0 / 1.96.0.
    """

    def test_an_unmapped_model_is_refused_its_tools(self):
        assert MID not in litellm.model_cost
        assert _params() is None

    def test_and_admitted_when_the_call_names_them(self):
        assert "tools" in (_params(["tools", "tool_choice"]) or [])

    def test_the_allow_list_only_ever_adds(self):
        """The property the declaration rests on: it cannot loosen anything."""
        assert _params(["tools", "tool_choice"], reasoning_effort="high") is None
        assert "reasoning_effort" in (
            _params(["tools", "tool_choice", "reasoning_effort"], reasoning_effort="high") or []
        )

    def test_completion_threads_the_kwarg_end_to_end(self):
        """get_optional_params accepting it is not enough; the call must too."""
        response = litellm.completion(
            model=MID,
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS,
            tool_choice="auto",
            mock_response="ok",
            api_key="unused",
            allowed_openai_params=["tools", "tool_choice"],
        )
        assert response.choices[0].message.content == "ok"

    def test_without_it_the_same_call_is_refused(self):
        with pytest.raises(litellm.UnsupportedParamsError):
            litellm.completion(
                model=MID,
                messages=[{"role": "user", "content": "hi"}],
                tools=TOOLS,
                tool_choice="auto",
                mock_response="ok",
                api_key="unused",
            )


class TestWhatAnEntryAdmits:
    def test_a_declared_flag_admits_its_parameters(self, overlay):
        overlay({MID: _entry()})
        assert allowed_openai_params(MID) == ["tools", "tool_choice"]

    def test_reasoning_is_admitted_only_when_declared(self, overlay):
        overlay({MID: _entry()})
        assert "reasoning_effort" not in allowed_openai_params(MID)
        overlay({MID: _entry(supports_reasoning=True)})
        assert "reasoning_effort" in allowed_openai_params(MID)

    def test_a_flag_declared_false_admits_nothing(self, overlay):
        overlay({MID: _entry(supports_function_calling=False, supports_reasoning=True)})
        assert allowed_openai_params(MID) == ["reasoning_effort"]

    def test_every_declarable_flag_admits_a_parameter_we_actually_send(self):
        """A flag admitting something the engine never sends is a dead knob.

        `tool_choice` only ever ships alongside `tools`, and
        `parallel_tool_calls` is never set, so a flag for either would validate
        cleanly and leave the run refused on the parameter it needed.
        """
        sent_somewhere = {"tools", "tool_choice", "reasoning_effort"}
        for flag in DECLARABLE_FLAGS:
            assert FLAG_PARAMS[flag], f"{flag} admits nothing"
            assert set(FLAG_PARAMS[flag]) <= sent_somewhere, f"{flag} admits an unsent parameter"


class TestTheEngineShipsNoList:
    """The capability is in the wheel; which models need it is operator data."""

    def test_no_overlay_means_nothing_is_admitted(self):
        set_overlay_path(None)
        assert litellm_model_entries() == {}
        assert allowed_openai_params(MID) == []

    def test_a_model_no_overlay_mentions_is_left_alone(self, overlay):
        overlay({MID: _entry()})
        assert allowed_openai_params("openrouter/deepseek/deepseek-v4-flash") == []

    def test_nothing_is_written_into_litellms_map(self, overlay):
        """No global mutation: no clobbering upstream, no cost relabelling."""
        before = dict(litellm.model_cost)
        overlay({MID: _entry(supports_reasoning=True)})
        allowed_openai_params(MID)
        assert litellm.model_cost == before
        assert MID not in litellm.model_cost


class TestAModelIdThatCarriesNoVendor:
    """The Nova path sends litellm a BARE name, with no `<provider>/` prefix."""

    def test_the_entry_is_found_via_the_provider(self, overlay):
        overlay({"nova/some-nova-model": _entry()})
        assert allowed_openai_params("some-nova-model", "nova") == ["tools", "tool_choice"]

    def test_without_a_provider_a_bare_id_matches_nothing(self, overlay):
        overlay({"nova/some-nova-model": _entry()})
        assert allowed_openai_params("some-nova-model") == []


class TestOverlayValidation:
    """Louder than the preset blocks around it: a dropped entry here does not
    change how a request is shaped, it decides whether one is sent at all."""

    @pytest.mark.parametrize(
        "entry, expected",
        [
            ({"supports_function_calling": True}, "evidence"),
            ({"supports_function_calling": True, "evidence": "  "}, "evidence"),
            ({"evidence": EVIDENCE, "like": "meta/muse-spark-1.1"}, "unknown keys"),
            ({"evidence": EVIDENCE, "input": 1.25}, "unknown keys"),
            ({"evidence": EVIDENCE, "supports_function_calling": "yes"}, "true or false"),
            ({"evidence": EVIDENCE}, "declares nothing"),
            ({"evidence": EVIDENCE, "supports_function_calling": False}, "declares nothing"),
        ],
    )
    def test_a_malformed_entry_fails_the_load(self, overlay, entry, expected):
        overlay({MID: entry})
        with pytest.raises(ValueError, match=expected):
            litellm_model_entries()

    @pytest.mark.parametrize("model_id", ["/some-model", "meta/", "/", "meta"])
    def test_a_key_that_is_not_a_litellm_model_id_fails(self, overlay, model_id):
        """The key IS the lookup, so a bare or half-bare name matches nothing."""
        overlay({model_id: _entry()})
        with pytest.raises(ValueError, match="provider./.model"):
            litellm_model_entries()

    def test_a_non_mapping_block_fails(self, tmp_path: Path):
        path = tmp_path / "overlay.yaml"
        path.write_text(yaml.safe_dump({"litellm_models": [MID]}))
        set_overlay_path(str(path))
        try:
            with pytest.raises(ValueError, match="must be a mapping"):
                litellm_model_entries()
        finally:
            set_overlay_path(None)


class TestCaseIsNotAWayToMiss:
    """One case rule on both sides of the lookup.

    The validator accepts any case, and the lookup lowercases the vendor. If
    only one of them did, a capitalised `Meta/...` in the YAML would validate,
    load, and then match nothing - producing the one report nobody can act on:
    the overlay is installed and the model still refuses its tools.
    """

    @pytest.mark.parametrize(
        "declared, asked",
        [
            ("Meta/muse-spark-1.2", "meta/muse-spark-1.2"),
            ("meta/muse-spark-1.2", "Meta/muse-spark-1.2"),
            ("META/muse-spark-1.2", "meta/muse-spark-1.2"),
        ],
    )
    def test_the_vendor_case_does_not_decide_whether_it_matches(self, overlay, declared, asked):
        overlay({declared: _entry()})
        assert allowed_openai_params(asked) == ["tools", "tool_choice"]

    def test_and_on_the_bare_name_path_too(self, overlay):
        overlay({"Nova/some-nova-model": _entry()})
        assert allowed_openai_params("some-nova-model", "NOVA") == ["tools", "tool_choice"]

    def test_the_model_name_keeps_its_own_case(self, overlay):
        """Only the vendor is normalised: litellm's ids are lowercase, model
        names are the vendor's business."""
        overlay({"meta/Muse-Spark-1.2": _entry()})
        assert allowed_openai_params("meta/Muse-Spark-1.2") == ["tools", "tool_choice"]
        assert allowed_openai_params("meta/muse-spark-1.2") == []


class TestTheClientActuallyAttachesIt:
    """The one line that connects the declaration to a request.

    Everything else here tests the list or tests litellm; without this, the
    whole feature could compute a list nobody reads and the suite would stay
    green while every declared model was still refused its tools.
    """

    def _kwargs(self, provider: str, name: str) -> dict:
        from tolokaforge.core.llm.client import LLMClient
        from tolokaforge.core.models.run_config import ModelConfig

        client = LLMClient(ModelConfig(provider=provider, name=name))
        return client._build_kwargs(
            system="You are a test.",
            messages=[],
            tools=TOOLS,
            tool_choice="auto",
            temperature=None,
            seed=None,
            reasoning=None,
            top_p=None,
            max_tokens=None,
        )

    def test_a_declared_model_carries_the_allow_list(self, overlay):
        overlay({MID: _entry()})
        assert self._kwargs("meta", NAME)["allowed_openai_params"] == ["tools", "tool_choice"]

    def test_a_model_litellm_knows_carries_no_kwarg_at_all(self, overlay):
        """Absent, not empty: an empty list is still a claim about the call."""
        overlay({MID: _entry()})
        assert "allowed_openai_params" not in self._kwargs(
            "openrouter", "deepseek/deepseek-v4-flash"
        )

    def test_what_is_declared_is_what_is_attached(self, overlay):
        overlay({MID: _entry(supports_reasoning=True)})
        assert self._kwargs("meta", NAME)["allowed_openai_params"] == [
            "tools",
            "tool_choice",
            "reasoning_effort",
        ]
