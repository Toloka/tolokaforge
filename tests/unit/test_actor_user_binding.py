"""``actors.user`` drives the user simulator; ``user_simulator`` is a
warning-emitting alias.

Every scenario writes a small ``task.yaml`` under ``tmp_path`` and drives
it through ``load_task_yaml`` — the same path the adapters use — then
asserts the resolved simulator ``TaskConfig.resolve_user_simulator``
returns (the value the conductor and native adapter read at runtime).
Fast: no Docker, no LLM, no filesystem outside tmp_path.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.adapters._task_loader import load_task_yaml

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f)


def _task_body(**extra: object) -> dict:
    body = {
        "task_id": "sample",
        "description": "sample task",
        "initial_state": {},
        "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
        "grading": "grading.yaml",
    }
    body.update(extra)
    return body


def _load(task_path: Path, **kwargs: object):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        task, _ = load_task_yaml(task_path, **kwargs)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    return task, deprecations


class TestActorsUserDrivesSimulator:
    def test_canonical_actors_user_reaches_resolved_simulator(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(actors={"user": {"mode": "llm", "persona": "curious engineer"}}),
        )
        task, deprecations = _load(task_path)
        sim = task.resolve_user_simulator()
        assert sim.mode == "llm"
        assert sim.persona == "curious engineer"
        assert deprecations == []

    def test_project_task_defaults_actors_user_reaches_resolved_simulator(
        self, tmp_path: Path
    ) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body())
        task, deprecations = _load(
            task_path,
            project_task_defaults={
                "actors": {"user": {"mode": "llm", "persona": "curious engineer"}}
            },
        )
        sim = task.resolve_user_simulator()
        assert sim.mode == "llm"
        assert sim.persona == "curious engineer"
        assert deprecations == []

    def test_legacy_user_simulator_resolves_identically_and_warns_once(
        self, tmp_path: Path
    ) -> None:
        canonical_path = tmp_path / "canon" / "task.yaml"
        _write_yaml(
            canonical_path,
            _task_body(actors={"user": {"mode": "scripted", "persona": "terse", "backstory": "b"}}),
        )
        canonical_task, _ = _load(canonical_path)

        legacy_path = tmp_path / "legacy" / "task.yaml"
        _write_yaml(
            legacy_path,
            _task_body(user_simulator={"mode": "scripted", "persona": "terse", "backstory": "b"}),
        )
        legacy_task, deprecations = _load(legacy_path)

        assert legacy_task.resolve_user_simulator() == canonical_task.resolve_user_simulator()
        assert len(deprecations) == 1
        assert "user_simulator" in str(deprecations[0].message)

    def test_neither_set_yields_default_simulator(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body())
        task, deprecations = _load(task_path)
        sim = task.resolve_user_simulator()
        assert task.actors is None
        assert sim.mode == "llm"
        assert sim.persona == "cooperative"
        assert deprecations == []

    def test_single_source_declaring_both_keys_fails_loud(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                actors={"user": {"mode": "llm"}},
                user_simulator={"mode": "scripted"},
            ),
        )
        with pytest.raises(ValueError, match="both top-level 'user_simulator' and 'actors.user'"):
            load_task_yaml(task_path)

    def test_mixed_shape_cross_layer_merges_delta_wins_with_one_warning(
        self, tmp_path: Path
    ) -> None:
        # Project declares actors.user {mode, persona}; the task carries a
        # legacy user_simulator that adds a backstory delta — the real
        # multi_service_postgres_reset / _lot_ops shape. Per-layer,
        # pre-merge canonicalisation must merge them (task's backstory wins)
        # with exactly one DeprecationWarning (the task's legacy key) and no
        # false single-source conflict.
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                user_simulator={
                    "mode": "llm",
                    "persona": "curious engineer",
                    "backstory": "I just joined the ops team.",
                }
            ),
        )
        task, deprecations = _load(
            task_path,
            project_task_defaults={
                "actors": {"user": {"mode": "llm", "persona": "curious engineer"}}
            },
        )
        sim = task.resolve_user_simulator()
        assert sim.mode == "llm"
        assert sim.persona == "curious engineer"
        assert sim.backstory == "I just joined the ops team."
        assert len(deprecations) == 1


class TestStopRuleDeclaration:
    """``actors.user.stop_tokens`` / ``stop_with_text`` reach the resolved
    simulator through every layer, and a list that cannot end a dialogue is
    refused at load rather than on the first trial."""

    _TAU_TOKENS = ["###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###"]
    # The built-in prompt teaches ###STOP### only; the backstory teaches the rest.
    _TAU_BACKSTORY = (
        "Send ###TRANSFER### once the agent transfers you, and ###OUT-OF-SCOPE### when "
        "the scenario does not say how to answer."
    )

    def test_declared_fields_reach_the_resolved_simulator(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                actors={
                    "user": {
                        "backstory": self._TAU_BACKSTORY,
                        "stop_tokens": self._TAU_TOKENS,
                        "stop_with_text": "end",
                    }
                }
            ),
        )
        sim = load_task_yaml(task_path)[0].resolve_user_simulator()
        assert sim.stop_tokens == self._TAU_TOKENS
        assert sim.stop_with_text == "end"

    def test_undeclared_fields_resolve_to_the_legacy_rule(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body(actors={"user": {"mode": "llm"}}))
        sim = load_task_yaml(task_path)[0].resolve_user_simulator()
        assert sim.stop_tokens == ["###STOP###"]
        assert sim.stop_with_text == "deliver"

    def test_a_project_list_and_a_task_mode_compose(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                actors={"user": {"backstory": self._TAU_BACKSTORY, "stop_with_text": "end"}}
            ),
        )
        task, _ = _load(
            task_path,
            project_task_defaults={"actors": {"user": {"stop_tokens": self._TAU_TOKENS}}},
        )
        sim = task.resolve_user_simulator()
        assert sim.stop_tokens == self._TAU_TOKENS
        assert sim.stop_with_text == "end"

    @pytest.mark.parametrize(
        ("tokens", "match"),
        [
            ([], "stop_tokens is empty"),
            (["###STOP###", " "], "blank token"),
            (["###STOP###", "###STOP###"], "more than once"),
            (["###STOP###", "###STOP"], "'###STOP' inside '###STOP###'"),
            (["###STOP###", "STOP"], "'STOP' inside '###STOP###'"),
        ],
    )
    def test_a_list_that_cannot_end_a_dialogue_is_refused(
        self, tmp_path: Path, tokens: list[str], match: str
    ) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body(actors={"user": {"stop_tokens": tokens}}))
        with pytest.raises(ValueError, match=match):
            load_task_yaml(task_path)

    def test_an_llm_simulator_without_the_prompted_token_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path, _task_body(actors={"user": {"mode": "llm", "stop_tokens": ["###DONE###"]}})
        )
        with pytest.raises(ValueError, match="built-in user-simulator prompt"):
            load_task_yaml(task_path)

    def test_the_prompted_token_is_required_whichever_layer_sets_the_mode(
        self, tmp_path: Path
    ) -> None:
        """The task sets only the list; the resolved mode defaults to ``llm``."""
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body(actors={"user": {"stop_tokens": ["###DONE###"]}}))
        with pytest.raises(ValueError, match="built-in user-simulator prompt"):
            load_task_yaml(task_path)

    def test_a_token_the_prompt_never_names_is_refused(self, tmp_path: Path) -> None:
        """Neither the built-in prompt nor this backstory tells the model to send
        ###TRANSFER###, so listening for it could never end a dialogue."""
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                actors={
                    "user": {
                        "backstory": "Move my booking to Friday.",
                        "stop_tokens": ["###STOP###", "###TRANSFER###"],
                    }
                }
            ),
        )
        with pytest.raises(ValueError, match=r"\['###TRANSFER###'\].*never told"):
            load_task_yaml(task_path)

    def test_a_scripted_simulator_may_use_any_token(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(actors={"user": {"mode": "scripted", "stop_tokens": ["###DONE###"]}}),
        )
        assert load_task_yaml(task_path)[0].resolve_user_simulator().stop_tokens == ["###DONE###"]

    def test_an_unknown_stop_with_text_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body(actors={"user": {"stop_with_text": "later"}}))
        with pytest.raises(ValueError, match="stop_with_text"):
            load_task_yaml(task_path)


class TestFirstMessageSpellingRefused:
    """An opener declared on the user actor is refused, whatever the spelling.

    The key is silently dropped otherwise — ``ActorSpec`` ignores extras and a
    nested key never reaches the loader's unknown-key warning — so the author
    would see neither their opener delivered nor a complaint.
    """

    def test_canonical_actors_user_spelling_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(actors={"user": {"mode": "llm", "first_message": "Hi, I need help."}}),
        )
        with pytest.raises(ValueError, match="initial_user_message"):
            load_task_yaml(task_path)

    def test_legacy_user_simulator_spelling_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(user_simulator={"mode": "llm", "first_message": "Hi, I need help."}),
        )
        with pytest.raises(ValueError, match="initial_user_message"):
            load_task_yaml(task_path)

    def test_project_task_defaults_spelling_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(task_path, _task_body())
        with pytest.raises(ValueError, match="initial_user_message"):
            load_task_yaml(
                task_path,
                project_task_defaults={"actors": {"user": {"first_message": "Hi."}}},
            )

    def test_direct_python_user_simulator_object_spelling_is_refused(self) -> None:
        """The kwarg shim's object form is refused too — the model refuses the key
        before ``TaskConfig`` model-dumps the instance into ``actors.user``."""
        from tolokaforge.core.models import TaskConfig, UserSimulatorConfig

        with pytest.raises(ValueError, match="initial_user_message"):
            TaskConfig(
                task_id="t1",
                description="d",
                user_simulator=UserSimulatorConfig(mode="llm", first_message="Hi, I need help."),
            )


class TestDirectPythonUserSimulatorKwargShim:
    """External Python callers doing ``TaskConfig(user_simulator=…)`` continue
    to work: a ``mode="before"`` shim on ``TaskConfig`` and ``TaskDefaults``
    lifts the legacy kwarg into ``actors["user"]`` with a
    ``DeprecationWarning``. YAML loads use the loader-side
    :func:`canonicalize_actor_config` on the same code path; this class covers
    the Python-only construction case.
    """

    def test_task_config_accepts_user_simulator_kwarg_with_warning(self) -> None:
        from tolokaforge.core.models import TaskConfig, UserSimulatorConfig

        with pytest.warns(DeprecationWarning, match="user_simulator"):
            task = TaskConfig(
                task_id="t1",
                description="d",
                user_simulator=UserSimulatorConfig(mode="llm", persona="curious"),
            )
        sim = task.resolve_user_simulator()
        assert sim.mode == "llm"
        assert sim.persona == "curious"
        # Legacy field is gone; canonical home is actors.user.
        assert task.actors is not None
        assert task.actors["user"].persona == "curious"

    def test_task_defaults_accepts_user_simulator_kwarg_with_warning(self) -> None:
        from tolokaforge.core.models import TaskDefaults, UserSimulatorConfig

        with pytest.warns(DeprecationWarning, match="user_simulator"):
            defaults = TaskDefaults(
                user_simulator=UserSimulatorConfig(mode="llm", persona="terse"),
            )
        assert defaults.actors is not None
        assert defaults.actors["user"].persona == "terse"

    def test_task_config_accepts_user_simulator_dict_kwarg(self) -> None:
        # A raw dict works too — same coercion path.
        from tolokaforge.core.models import TaskConfig

        with pytest.warns(DeprecationWarning, match="user_simulator"):
            task = TaskConfig(
                task_id="t1",
                description="d",
                user_simulator={"mode": "llm", "persona": "polite"},
            )
        assert task.actors is not None
        assert task.actors["user"].persona == "polite"

    def test_task_config_without_user_simulator_kwarg_no_warning(self) -> None:
        # No legacy kwarg → no warning fires.
        from tolokaforge.core.models import TaskConfig

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any DeprecationWarning becomes an error
            task = TaskConfig(task_id="t1", description="d")
        assert task.actors is None


class TestUserToolsNeedATurnThatCanCallThem:
    """``tools.user.enabled`` is a claim that the user actor calls those tools.

    The declaration reaches the runner either way — the tools are registered for
    the trial like the agent's — so a pack whose user turn cannot make a call
    fails nothing at run time and grades a ``requestor: user`` action against a
    call that could not have happened, on every trial. Two shapes reach that
    state, and each is refused at load naming which one it is.
    """

    def test_a_user_tool_under_agent_only_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                interaction_mode="agent_only",
                tools={"agent": {"enabled": []}, "user": {"enabled": ["calculator"]}},
            ),
        )

        with pytest.raises(ValidationError, match="dispatches no user turn at all"):
            _load(task_path)

    def test_a_user_tool_under_a_scripted_simulator_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                actors={"user": {"mode": "scripted"}},
                tools={"agent": {"enabled": []}, "user": {"enabled": ["calculator"]}},
            ),
        )

        with pytest.raises(ValidationError, match="never a tool call"):
            _load(task_path)

    def test_the_same_declaration_loads_under_a_conversational_llm_simulator(
        self, tmp_path: Path
    ) -> None:
        """The row that makes the two above about the shape rather than the key."""
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                interaction_mode="conversational",
                actors={"user": {"mode": "llm"}},
                tools={"agent": {"enabled": []}, "user": {"enabled": ["calculator"]}},
            ),
        )

        task, _ = _load(task_path)

        assert task.tools.user["enabled"] == ["calculator"]

    def test_an_empty_declaration_loads_under_both_refused_shapes(self, tmp_path: Path) -> None:
        """Every pack in the tree declares ``tools.user.enabled: []``, under both."""
        for label, extra in (
            ("agent_only", {"interaction_mode": "agent_only"}),
            ("scripted", {"actors": {"user": {"mode": "scripted"}}}),
        ):
            task_path = tmp_path / label / "task.yaml"
            _write_yaml(task_path, _task_body(**extra))
            task, _ = _load(task_path)
            assert task.tools.user["enabled"] == []


class TestOneTaskShipsOneMcpServer:
    """A second MCP server has nowhere to put its schemas.

    Resolution reads ``<task_dir>/fixtures/tools.json``, which is keyed on the task
    and not on the server, so a user block naming its own server resolves against
    the agent server's fixture: the simulator would be offered tools that do not
    exist, and a grading rule naming one would be checked against another tool's
    arguments. The pack is refused where the two names are written rather than
    resolved into the wrong answer.
    """

    def test_a_user_block_naming_a_second_server_is_refused(self, tmp_path: Path) -> None:
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                tools={
                    "agent": {"mcp_server": "mcp_server.py", "enabled": ["write_file"]},
                    "user": {"mcp_server": "user_server.py", "enabled": ["calculator"]},
                },
            ),
        )

        with pytest.raises(ValidationError, match="A task ships one MCP server"):
            _load(task_path)

    def test_both_blocks_naming_one_server_loads(self, tmp_path: Path) -> None:
        """The control: it is the second *name* that is refused, not a user block
        with a server."""
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                tools={
                    "agent": {"mcp_server": "mcp_server.py", "enabled": ["write_file"]},
                    "user": {"mcp_server": "mcp_server.py", "enabled": ["read_meter"]},
                },
            ),
        )

        task, _ = _load(task_path)

        assert task.tools.user["mcp_server"] == task.tools.agent["mcp_server"]

    def test_a_user_only_server_loads(self, tmp_path: Path) -> None:
        """One server is one server whichever block names it."""
        task_path = tmp_path / "task.yaml"
        _write_yaml(
            task_path,
            _task_body(
                tools={
                    "agent": {"enabled": []},
                    "user": {"mcp_server": "user_server.py", "enabled": ["read_meter"]},
                },
            ),
        )

        task, _ = _load(task_path)

        assert task.tools.user["mcp_server"] == "user_server.py"
