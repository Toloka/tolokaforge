"""The default trace vocabulary (ADR-0047, vocabulary amendment): prefixes, derived tags, the
environment rule; shared with the offline uploader through this package."""

from __future__ import annotations

import pytest

from tolokaforge_langfuse import vocabulary as v

pytestmark = pytest.mark.unit


class TestPrefixes:
    def test_producer_and_caller_prefixes_partition_the_core(self) -> None:
        assert set(v.PRODUCER_PREFIXES) <= set(v.CORE_PREFIXES)
        assert set(v.CALLER_PREFIXES) | set(v.PRODUCER_PREFIXES) == set(v.CORE_PREFIXES)
        assert v.CALLER_PREFIXES == (
            "team",
            "dataset",
            "run_kind",
            "scope",
            "config",
            "domain",
            "ci_run",
            "ci_chain",
        )
        # every derived prefix is the producer's: a caller can never contradict a bundle fact
        for facet in v.MODEL_FACETS:
            assert f"model_{facet}" in v.PRODUCER_PREFIXES
        assert {"reasoning_mode", "reasoning_effort", "reasoning_budget", "route"} <= set(
            v.PRODUCER_PREFIXES
        )

    def test_the_core_lists_are_the_engines_own_words(self) -> None:
        assert v.CORE_VALUES == {
            "run_kind": ("eval", "smoke", "canary", "test", "probe"),
            "scope": ("full", "sample"),
        }

    @pytest.mark.parametrize(
        ("tag", "message"),
        [
            ("demo", "must look like"),
            ("Config:stem", "lowercase"),
            ("a:b c", "without whitespace"),
            ("model:x/y", "set by the producer"),
            ("task:T-1", "set by the producer"),
            ("route:openrouter", "set by the producer"),
            ("campaign:x", "not a core prefix"),
            ("run_kind:training", "must be one of"),
            ("project:pilot", "set by the producer"),
        ],
    )
    def test_caller_tags_are_refused_by_name(self, tag: str, message: str) -> None:
        with pytest.raises(v.VocabularyError, match=message):
            v.validate_caller_tag(tag)

    def test_the_launcher_may_pass_the_receivers_project(self) -> None:
        assert v.validate_caller_tag("project:pilot", launcher=True) == "project:pilot"
        assert v.validate_caller_tag("dataset:v9") == "dataset:v9"  # free-form without a profile
        with pytest.raises(v.VocabularyError, match="must be one of"):
            v.validate_caller_tag("dataset:v9", profile_values={"dataset": ("v1",)})

    def test_order_follows_the_core_and_deduplicates(self) -> None:
        assert v.order_tags(["task:T", "model:a/b", "team:x", "route:r", "team:x"]) == [
            "team:x",
            "model:a/b",
            "task:T",
            "route:r",
        ]


class TestDerivedTags:
    TASK = {
        "model_config": {
            "agent": {
                "provider": "openrouter",
                "name": "acme/pilot-1",
                "reasoning": {
                    "mode": "adaptive",
                    "effort_hint": "medium",
                    "budget_tokens": None,
                    "display": "visible",
                },
            }
        }
    }

    def test_facets_become_tags_only_when_the_rules_derived_a_value(self) -> None:
        fields = {"vendor": "acme", "family": "pilot", "generation": "3.7", "tier": "flash"}
        assert v.facet_tags(fields) == ["model_generation:3.7", "model_tier:flash"]
        assert v.facet_tags({"generation": None, "tier": ""}) == []
        assert v.facet_tags({"size": "120b", "snapshot": "0731", "stage": "preview"}) == [
            "model_size:120b",
            "model_stage:preview",
            "model_snapshot:0731",
        ]

    def test_reasoning_and_route_from_the_bundle(self) -> None:
        metrics = {"usage": {"calls": [{"cost_source": "local"}, {"cost_source": "unknown"}]}}
        assert v.derived_tags(self.TASK, metrics) == [
            "reasoning_mode:adaptive",
            "reasoning_effort:medium",
            "route:openrouter",
        ]

    def test_a_budget_and_the_route_is_the_configured_provider(self) -> None:
        task = {
            "model_config": {
                "agent": {
                    "provider": "openrouter",
                    "name": "acme/pilot-1",
                    "reasoning": {"mode": "budget", "budget_tokens": 8000},
                }
            }
        }
        # cost_source names the engine's cost calculator, not a transport: a gateway in front of
        # the provider leaves no mark in the bundle, so the route stays the configured provider
        # (a live run of 2026-09-17 direct to the provider recorded cost_source litellm)
        calculator = {"usage": {"calls": [{"cost_source": "litellm"}, {"cost_source": "litellm"}]}}
        assert v.derived_tags(task, calculator) == [
            "reasoning_mode:budget",
            "reasoning_budget:8000",
            "route:openrouter",
        ]

    def test_groups_switch_off_and_nothing_is_invented(self) -> None:
        assert v.derived_tags(self.TASK, {}, groups=("route",)) == ["route:openrouter"]
        assert v.derived_tags(self.TASK, {}, groups=()) == []
        assert v.derived_tags({}, {}) == []
        assert v.derived_tags({"model_config": {"agent": "claude-code"}}, {}) == []
        harness = {"model_config": {"agent": "claude-code", "model_info": {"provider": "acme"}}}
        assert v.derived_tags(harness, {}) == ["route:acme"]
        bad = {"model_config": {"agent": {"provider": "open router", "reasoning": {"mode": "a b"}}}}
        assert v.derived_tags(bad, {}) == []


class TestEnvironmentRule:
    def test_the_default_rule_is_production_for_an_evaluation(self) -> None:
        assert v.environment_for(["run_kind:eval"]) == "production"
        assert v.environment_for(["run_kind:smoke"]) == "development"
        assert v.environment_for([]) == "development"
        assert v.DEFAULT_ENVIRONMENT_RULE.resolve(["team:x"]) == "development"


class TestTraceName:
    TAGS = ["team:delivery", "domain:ots_19_airlines", "dataset:v1", "task:ANC-001"]

    def test_a_template_names_the_trace_from_its_tags(self) -> None:
        assert v.trace_name("{dataset}/{domain}", self.TAGS, label="x") == "v1/ots_19_airlines"

    def test_without_a_template_the_run_and_the_task_name_it(self) -> None:
        assert v.trace_name(None, self.TAGS, label="opus_48") == "opus_48/ANC-001"

    def test_the_first_value_of_each_prefix_counts(self) -> None:
        tags = ["dataset:v3", "dataset:v1", "domain:a", "domain:b", "task:t"]
        assert v.trace_name("{dataset}/{domain}", tags, label="x") == "v3/a"

    def test_a_value_the_trace_lacks_gives_the_default_name(self) -> None:
        assert v.trace_name("{dataset}/{domain}", ["dataset:v1", "task:t"], label="l") == "l/t"
        assert v.trace_name(None, [], label="l") == "l"

    @pytest.mark.parametrize("template", ["{dataset}/{domain}", "{label}", "arena {scope}"])
    def test_a_template_over_prefixes_and_the_label_is_valid(self, template: str) -> None:
        assert v.check_trace_name(template) == template

    @pytest.mark.parametrize("template", ["", "{expert}", "{dataset", "{Domain}"])
    def test_anything_else_is_refused(self, template: str) -> None:
        with pytest.raises(v.VocabularyError):
            v.check_trace_name(template)


class TestTraceUser:
    def test_by_default_a_trace_has_no_user(self) -> None:
        assert v.trace_user(v.TRACE_USER_NONE, "acme/pilot-1") is None

    def test_the_model_source_makes_the_agents_identity_the_user(self) -> None:
        assert v.trace_user(v.TRACE_USER_MODEL, "acme/pilot-1") == "acme/pilot-1"

    def test_an_unknown_source_is_refused(self) -> None:
        with pytest.raises(v.VocabularyError):
            v.check_trace_user("expert")


class TestGenerationName:
    @pytest.mark.parametrize(
        ("role", "name"),
        [("agent", "agent"), ("user", "user simulator"), ("summarizer", "summarizer")],
    )
    def test_a_generation_is_named_after_its_actor(self, role: str, name: str) -> None:
        assert v.generation_name(role) == name
