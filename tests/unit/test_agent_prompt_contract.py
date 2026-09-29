"""A reply contract composes with a task's own prompt, and never replaces it silently."""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters._task_loader import load_task
from tolokaforge.core.agent_prompt_contract import (
    CONTRACTS,
    UnknownAgentPromptContractError,
    resolve_agent_prompt_contract,
)
from tolokaforge.core.llm import build_capabilities, presets
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.system_prompt import build_system_prompt

pytestmark = pytest.mark.unit


def _task(**kwargs: object) -> TaskConfig:
    return TaskConfig(task_id="t", description="d", **kwargs)  # type: ignore[arg-type]


class TestResolvingAContract:
    def test_a_shipped_name_returns_its_text(self, tmp_path: Path) -> None:
        assert resolve_agent_prompt_contract("reasoning_agent", task_dir=tmp_path) == (
            CONTRACTS["reasoning_agent"]
        )

    def test_a_pack_may_ship_its_own_beside_the_task(self, tmp_path: Path) -> None:
        (tmp_path / "house_style.md").write_text("Answer in limericks.")

        assert (
            resolve_agent_prompt_contract("house_style.md", task_dir=tmp_path)
            == "Answer in limericks."
        )

    def test_an_unknown_name_is_refused_rather_than_ignored(self, tmp_path: Path) -> None:
        """A silently dropped selector would run a whole task set on the wrong prompt."""
        with pytest.raises(UnknownAgentPromptContractError) as excinfo:
            resolve_agent_prompt_contract("no_such_contract", task_dir=tmp_path)

        assert "reasoning_agent" in str(excinfo.value), "the error names what is available"

    def test_an_empty_file_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "blank.md").write_text("   \n")

        with pytest.raises(UnknownAgentPromptContractError):
            resolve_agent_prompt_contract("blank.md", task_dir=tmp_path)


class TestWhereAContractSitsInThePriorityChain:
    def test_it_does_not_fire_when_nothing_selects_one(self, tmp_path: Path) -> None:
        """The chain that predates contracts is untouched by their existence."""
        assert build_system_prompt(task=_task(), task_dir=tmp_path) == (
            "You are a helpful assistant."
        )

    def test_a_task_selects_one_by_name(self, tmp_path: Path) -> None:
        built = build_system_prompt(
            task=_task(agent_prompt_contract="reasoning_agent"), task_dir=tmp_path
        )

        assert built.startswith(CONTRACTS["reasoning_agent"])

    def test_a_preset_default_supplies_one_the_task_did_not(self, tmp_path: Path) -> None:
        from_default = build_system_prompt(
            task=_task(interaction_mode="agent_only"),
            task_dir=tmp_path,
            default_prompt_contract="reasoning_agent",
        )
        from_task = build_system_prompt(
            task=_task(agent_prompt_contract="reasoning_agent"), task_dir=tmp_path
        )

        assert from_default == from_task

    def test_the_preset_default_stays_out_of_a_conversation(self, tmp_path: Path) -> None:
        """The contract says a tool-call-free message ends the task.

        That is the solo turn policy's rule. In a conversation the same message
        hands the floor to the user, so a blanket default must not reach one.
        """
        built = build_system_prompt(
            task=_task(interaction_mode="conversational"),
            task_dir=tmp_path,
            default_prompt_contract="reasoning_agent",
        )

        assert built == "You are a helpful assistant."

    def test_a_task_may_still_name_one_in_a_conversation(self, tmp_path: Path) -> None:
        """Naming it is an author's decision about one task; the default is a blanket."""
        built = build_system_prompt(
            task=_task(interaction_mode="conversational", agent_prompt_contract="reasoning_agent"),
            task_dir=tmp_path,
        )

        assert built.startswith(CONTRACTS["reasoning_agent"])

    def test_the_task_wins_over_the_preset_default(self, tmp_path: Path) -> None:
        (tmp_path / "mine.md").write_text("MINE")

        built = build_system_prompt(
            task=_task(agent_prompt_contract="mine.md", interaction_mode="agent_only"),
            task_dir=tmp_path,
            default_prompt_contract="reasoning_agent",
        )

        assert built.startswith("MINE")

    def test_an_explicit_prompt_still_outranks_every_contract(self, tmp_path: Path) -> None:
        """``agent_system_prompt`` reproduces a prompt byte for byte; nothing may pad it."""
        built = build_system_prompt(
            task=_task(
                agent_prompt_contract="reasoning_agent",
                interaction_mode="agent_only",
                policies={"agent_system_prompt": "VERBATIM"},
            ),
            task_dir=tmp_path,
            default_prompt_contract="reasoning_agent",
        )

        assert built == "VERBATIM"


class TestComposition:
    def test_the_task_keeps_its_own_guidance(self, tmp_path: Path) -> None:
        built = build_system_prompt(
            task=_task(
                agent_prompt_contract="reasoning_agent",
                policies={"guidance": ["Never drop the database."]},
            ),
            task_dir=tmp_path,
        )

        assert CONTRACTS["reasoning_agent"] in built
        assert "Never drop the database." in built

    def test_the_contract_comes_first(self, tmp_path: Path) -> None:
        """The standing rule precedes the specific job, for a model reading top-down."""
        built = build_system_prompt(
            task=_task(
                agent_prompt_contract="reasoning_agent",
                policies={"guidance": ["Never drop the database."]},
            ),
            task_dir=tmp_path,
        )

        assert built.index("expert software engineer") < built.index("Never drop the database.")

    def test_the_generic_persona_is_dropped_so_two_do_not_collide(self, tmp_path: Path) -> None:
        built = build_system_prompt(
            task=_task(agent_prompt_contract="reasoning_agent"), task_dir=tmp_path
        )

        assert "You are a helpful assistant." not in built
        assert "You are an expert software engineer" in built


class TestWhatTheShippedContractMustSay:
    """Each assertion pins a property a live run depends on, not prose taste."""

    def test_it_pairs_the_note_with_the_tool_call(self) -> None:
        assert "in the same turn" in CONTRACTS["reasoning_agent"]

    def test_it_forbids_a_lone_message_while_working(self) -> None:
        """Under ``agent_only`` a bare prose turn ends the episode on an untouched container."""
        assert "Never send a message on its own" in CONTRACTS["reasoning_agent"]

    def test_it_describes_completion_structurally_and_names_no_token(self) -> None:
        contract = CONTRACTS["reasoning_agent"]

        assert "make no tool" in contract
        assert "###STOP###" not in contract, "the exit token belongs to the user simulator"

    def test_it_asks_the_model_to_reconcile_against_what_it_expected(self) -> None:
        assert "what you expect it to produce" in CONTRACTS["reasoning_agent"]

    def test_it_stays_short_enough_not_to_spend_the_cost_advantage(self) -> None:
        """Re-sent every turn, so length is a per-turn tax on every trial."""
        assert len(CONTRACTS["reasoning_agent"]) < 2_000


class TestThePresetKnob:
    """``default_agent_prompt_contract`` is preset data, read like any other knob."""

    def test_a_preset_block_declaring_one_reaches_capabilities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            presets,
            "_match_preset",
            lambda *_args, **_kwargs: {"default_agent_prompt_contract": "reasoning_agent"},
        )

        caps = build_capabilities("acme/widget-1", "openrouter")

        assert caps.default_agent_prompt_contract == "reasoning_agent"

    def test_a_preset_that_names_none_leaves_the_slot_empty(self) -> None:
        caps = build_capabilities("openai/gpt-5.6-sol", "openrouter")

        assert caps.default_agent_prompt_contract is None


class TestLoadingAPack:
    """A typo is refused at load, not at the first trial's prompt build."""

    def _write(self, tmp_path: Path, selector: str) -> Path:
        path = tmp_path / "task.yaml"
        path.write_text("task_id: t\ndescription: d\nagent_prompt_contract: " + selector + "\n")
        return path

    def test_an_unknown_name_is_refused_before_anything_is_provisioned(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(UnknownAgentPromptContractError):
            load_task(self._write(tmp_path, "no_such_contract"))

    def test_a_shipped_name_loads(self, tmp_path: Path) -> None:
        task = load_task(self._write(tmp_path, "reasoning_agent"))

        assert task.agent_prompt_contract == "reasoning_agent"

    def test_a_file_beside_the_task_loads(self, tmp_path: Path) -> None:
        (tmp_path / "house_style.md").write_text("Answer in limericks.")

        task = load_task(self._write(tmp_path, "house_style.md"))

        assert task.agent_prompt_contract == "house_style.md"
