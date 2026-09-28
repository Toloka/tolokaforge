"""Preset fall-through audit — detector shape and engine-agreement locks.

The audit's whole value is that it names hazards nobody wrote down: a model
whose resolved preset carries none of the budget knobs its near-twin carries,
and a context ceiling that disagrees with the provider's real window. Both
detectors are structural, so the tests drive synthetic preset tables through
them rather than asserting on the shipped one — a shipped-data assertion would
turn a preset fix into a test failure.

Two things are pinned against the shipped data instead, because they are
contracts rather than findings:

* the audit's glob matcher agrees with
  :func:`~tolokaforge.core.llm.presets.resolve_effective_preset` on every slug
  the default run covers (the matcher is a deliberate mirror of an engine
  private, and a silent drift would make every "which slugs does this preset
  cover?" answer wrong);
* the audit reads only knobs that exist on
  :class:`~tolokaforge.core.llm.capabilities.ModelCapabilities`.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.analysis.audit_preset_fallthrough import (
    KNOB_IMPACT,
    KNOBS,
    CeilingFinding,
    ModelRow,
    WindowLookup,
    concrete_slug,
    context_windows,
    default_slugs,
    family_of,
    find_ceilings,
    find_suspects,
    glob_vendor,
    harvest_slugs,
    load_openrouter_models,
    lookup_window,
    main,
    normalized_vendor,
    overshoots,
    preset_matches,
    preset_vendor_map,
    vendor_of,
)
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.presets import resolve_effective_preset

pytestmark = pytest.mark.unit


@contextmanager
def _synthetic_fallthrough():
    """Drive the CLI over a preset table that exhibits one SUSPECT.

    ``acme/thing-1`` lands on a preset whose globs span two vendors and that
    declares no budget knobs; its same-family sibling ``acme/thing-2`` lands
    on a preset that declares two. That pair is the shape the SUSPECT rule
    names, built here rather than borrowed from the shipped table so that
    fixing a real fall-through does not fail this test.
    """
    presets = {
        "presets": {
            "acme_pro": {
                "match": ["acme/thing-2"],
                "default_max_turns": 90,
                "empty_retry_count": 3,
            },
            "shared_wire": {"match": ["acme/*", "othercorp/*"]},
        }
    }
    resolved = {"acme/thing-2": "acme_pro", "acme/thing-1": "shared_wire"}
    caps = SimpleNamespace(**dict.fromkeys(KNOBS))
    with (
        patch(
            "scripts.analysis.audit_preset_fallthrough.get_resolved_presets",
            return_value=presets,
        ),
        patch(
            "scripts.analysis.audit_preset_fallthrough.resolve_effective_preset",
            side_effect=lambda slug, _provider="": resolved[slug],
        ),
        patch(
            "scripts.analysis.audit_preset_fallthrough.build_capabilities",
            return_value=caps,
        ),
    ):
        yield


def _row(slug: str, preset: str, declared: dict[str, object]) -> ModelRow:
    return ModelRow(
        slug=slug,
        preset=preset,
        family=family_of(slug),
        vendor=vendor_of(slug),
        declared=declared,
        effective={},
        window=WindowLookup(None, exact=False),
    )


# ---------------------------------------------------------------------------
# Knob set
# ---------------------------------------------------------------------------


class TestKnobSet:
    def test_every_audited_knob_exists_on_capabilities(self):
        """A knob the audit names but the dataclass dropped would silently read
        as 'unset' on every model — the audit would report all-clear on a
        renamed field."""
        attrs = {f.name for f in fields(ModelCapabilities)}
        assert set(KNOBS) <= attrs, f"unknown knobs: {set(KNOBS) - attrs}"

    def test_every_knob_has_an_impact_line(self):
        """The ranked SUSPECT list is only actionable if each missing knob says
        what it costs."""
        assert set(KNOB_IMPACT) == set(KNOBS)

    def test_column_codes_are_unique(self):
        assert len(set(KNOBS.values())) == len(KNOBS)


# ---------------------------------------------------------------------------
# Slug harvesting / family grouping
# ---------------------------------------------------------------------------


class TestConcreteSlug:
    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            ("moonshotai/kimi-k3*", "moonshotai/kimi-k3"),
            ("deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash-0731"),
            ("openrouter/google/gemini-3.5-flash", "openrouter/google/gemini-3.5-flash"),
            ("azure_ai/cohere-command-a-plus*", "azure_ai/cohere-command-a-plus"),
        ],
    )
    def test_concrete_patterns_yield_a_slug(self, pattern, expected):
        assert concrete_slug(pattern) == expected

    @pytest.mark.parametrize(
        "pattern",
        [
            "*kimi-k2*",  # leading wildcard
            "*/x-ai/grok-4.6*",  # interior wildcard
            "qwen/*",  # vendor-only
            "google/gemini-*",  # prefix stub ending on a separator
            "grok*",  # no vendor separator
            "nova*",
        ],
    )
    def test_wildcards_are_skipped(self, pattern):
        assert concrete_slug(pattern) is None

    def test_harvest_dedupes_across_presets(self):
        presets = {
            "presets": {
                "a": {"match": ["vendor/model-1*", "*model-1*"]},
                "b": {"match": ["vendor/model-1", "vendor/model-2"]},
            }
        }
        assert harvest_slugs(presets) == ["vendor/model-1", "vendor/model-2"]


class TestFamilyGrouping:
    @pytest.mark.parametrize(
        ("slug", "family"),
        [
            # The shape the audit exists for: two slugs, one family, no
            # hardcoded vendor name anywhere in the rule.
            ("moonshotai/kimi-k2.7-code", "moonshotai/kimi"),
            ("moonshotai/kimi-k3", "moonshotai/kimi"),
            ("anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet"),
            ("anthropic/claude-opus-5", "anthropic/claude-opus"),
            ("google/gemini-3.1-pro-preview", "google/gemini"),
            ("z-ai/glm-5.1", "z-ai/glm"),
            # A first token that already carries a digit is kept, or the
            # family would be the empty string.
            ("openai/gpt-5.6-sol", "openai/gpt"),
            ("minimax/minimax-m3", "minimax/minimax"),
        ],
    )
    def test_family_strips_the_version_token_onwards(self, slug, family):
        assert family_of(slug) == family

    def test_gateway_prefix_does_not_become_the_vendor(self):
        assert vendor_of("openrouter/google/gemini-3.5-flash") == "google"
        assert family_of("openrouter/google/gemini-3.5-flash") == "google/gemini"


class TestPresetVendors:
    @pytest.mark.parametrize(
        ("pattern", "vendor"),
        [
            ("moonshotai/kimi-k2*", "moonshotai"),
            ("*/z-ai/glm-5.3", "z-ai"),
            ("*amazon/nova*", "amazon"),
            ("openrouter/google/gemini-*", "google"),
        ],
    )
    def test_glob_names_its_vendor(self, pattern, vendor):
        assert glob_vendor(pattern) == vendor

    @pytest.mark.parametrize("pattern", ["*kimi-k2*", "*claude*", "grok*", "nova*"])
    def test_vendorless_globs_name_none(self, pattern):
        """A glob any vendor's slug could satisfy contributes no vendor — it
        would otherwise make every broad preset look multi-vendor."""
        assert glob_vendor(pattern) is None

    def test_alias_spellings_collapse_to_one_vendor(self):
        """``x-ai/*`` plus ``xai/*`` is one vendor under two spellings, not a
        shared preset."""
        assert normalized_vendor("x-ai") == normalized_vendor("xai")
        vendors = preset_vendor_map({"presets": {"p": {"match": ["x-ai/*", "xai/*", "grok*"]}}})
        assert vendors["p"] == {"xai"}

    def test_multi_vendor_preset_is_seen_from_its_globs_alone(self):
        vendors = preset_vendor_map(
            {"presets": {"p": {"match": ["acme/widget*", "*gadget*", "othercorp/thing*"]}}}
        )
        assert vendors["p"] == {"acme", "othercorp"}

    def test_shipped_shared_preset_is_multi_vendor(self):
        """Pins the property the SUSPECT rule turns on, without pinning which
        models sit behind it."""
        from tolokaforge.core.llm.presets import get_resolved_presets

        vendors = preset_vendor_map(get_resolved_presets())
        shared = [name for name, v in vendors.items() if len(v) > 1]
        assert shared, "no preset in the shipped table spans more than one vendor"

    def test_glob_derived_sharedness_survives_a_single_vendor_run(self):
        """A two-slug run over one vendor must still see that the preset it
        lands on answers to other vendors — otherwise the finding degrades to
        DRIFT exactly when the operator narrows the audit to the models they
        care about."""
        rows = [
            _row("acme/widget-1", "shared", {}),
            _row("acme/widget-2", "acme_2", {"empty_retry_count": 1}),
        ]
        assert find_suspects(rows)[0] == []
        suspects, drifts = find_suspects(rows, {"shared": {"acme", "othercorp"}})
        assert [s.slug for s in suspects] == ["acme/widget-1"]
        assert drifts == []


# ---------------------------------------------------------------------------
# SUSPECT detection
# ---------------------------------------------------------------------------


class TestFindSuspects:
    def test_shared_preset_plus_richer_sibling_is_suspect(self):
        """The kimi-k2 vs kimi-k3 shape, spelled with invented vendors so the
        detector cannot be passing on a memorised name."""
        rows = [
            _row("acme/widget-1", "shared_wire_shape", {}),
            _row("acme/widget-2", "acme_widget_2", {"empty_retry_count": 1}),
            _row("othercorp/gadget-9", "shared_wire_shape", {}),
        ]
        suspects, drifts = find_suspects(rows)
        assert [s.slug for s in suspects] == ["acme/widget-1"]
        assert suspects[0].missing == ("empty_retry_count",)
        assert suspects[0].sibling_slug == "acme/widget-2"
        assert suspects[0].shared_with_vendors == ("acme", "othercorp")
        assert drifts == []

    def test_single_vendor_preset_is_drift_not_suspect(self):
        rows = [
            _row("acme/widget-1", "acme_generic", {}),
            _row("acme/widget-2", "acme_widget_2", {"max_context_tokens": 1}),
        ]
        suspects, drifts = find_suspects(rows)
        assert suspects == []
        assert [d.slug for d in drifts] == ["acme/widget-1"]
        assert drifts[0].missing == ("max_context_tokens",)

    def test_shared_preset_with_no_richer_sibling_is_not_flagged(self):
        """Sharing a preset across vendors is the normal case for a wire-shape
        preset. Only the asymmetry against a sibling makes it a finding."""
        rows = [
            _row("acme/widget-1", "shared_wire_shape", {}),
            _row("othercorp/gadget-9", "shared_wire_shape", {}),
            _row("acme/widget-2", "acme_widget_2", {}),
        ]
        suspects, drifts = find_suspects(rows)
        assert suspects == []
        assert drifts == []

    def test_sibling_on_the_same_preset_is_not_a_comparison(self):
        rows = [
            _row("acme/widget-1", "shared", {"empty_retry_count": 1}),
            _row("acme/widget-2", "shared", {"empty_retry_count": 1}),
            _row("othercorp/gadget-9", "shared", {"empty_retry_count": 1}),
        ]
        assert find_suspects(rows) == ([], [])

    def test_ranking_puts_the_widest_gap_first(self):
        rows = [
            _row("acme/widget-1", "shared", {}),
            _row("acme/widget-2", "acme_2", {"empty_retry_count": 1}),
            _row("beta/thing-1", "shared", {}),
            _row(
                "beta/thing-2",
                "beta_2",
                {"empty_retry_count": 1, "max_context_tokens": 1, "context_watermark": 1},
            ),
        ]
        suspects, _ = find_suspects(rows)
        assert [s.slug for s in suspects] == ["beta/thing-1", "acme/widget-1"]
        assert len(suspects[0].missing) == 3

    def test_richest_sibling_wins_the_comparison(self):
        rows = [
            _row("acme/widget-1", "shared", {}),
            _row("acme/widget-2", "acme_2", {"empty_retry_count": 1}),
            _row(
                "acme/widget-3",
                "acme_3",
                {"empty_retry_count": 1, "tool_output_max_chars": 1},
            ),
            _row("othercorp/gadget-9", "shared", {}),
        ]
        suspects, _ = find_suspects(rows)
        assert suspects[0].sibling_slug == "acme/widget-3"
        assert suspects[0].missing == ("empty_retry_count", "tool_output_max_chars")

    def test_missing_knobs_follow_the_declared_column_order(self):
        rows = [
            _row("acme/widget-1", "shared", {}),
            _row(
                "acme/widget-2",
                "acme_2",
                {"tool_output_max_chars": 1, "default_max_turns": 1, "empty_retry_count": 1},
            ),
            _row("othercorp/gadget-9", "shared", {}),
        ]
        suspects, _ = find_suspects(rows)
        assert suspects[0].missing == (
            "default_max_turns",
            "empty_retry_count",
            "tool_output_max_chars",
        )


# ---------------------------------------------------------------------------
# Glob matcher mirror
# ---------------------------------------------------------------------------


class TestPresetMatcher:
    def test_matcher_agrees_with_engine_resolution(self):
        """The audit's matcher answers "which slugs does this preset cover?",
        which the engine's first-match-wins accessors cannot. It is a mirror of
        an engine private, so agreement is pinned: for every slug in the default
        run, the FIRST preset this matcher accepts must be the preset the engine
        resolves to."""
        from tolokaforge.core.llm.presets import get_resolved_presets

        blocks = get_resolved_presets()["presets"]
        for slug in default_slugs():
            mine = next(
                (name for name, b in blocks.items() if preset_matches(b, slug, "openrouter")),
                "default",
            )
            assert mine == resolve_effective_preset(slug, "openrouter"), slug

    def test_match_provider_is_honoured(self):
        block = {"match": ["nova*"], "match_provider": ["nova"]}
        assert preset_matches(block, "unrelated/model", "nova")
        assert not preset_matches(block, "unrelated/model", "openrouter")

    def test_matching_is_case_insensitive_on_the_slug(self):
        block = {"match": ["acme/widget*"]}
        assert preset_matches(block, "ACME/Widget-1", "")


# ---------------------------------------------------------------------------
# Context windows
# ---------------------------------------------------------------------------


class TestWindowLookup:
    def test_exact_id_wins(self):
        windows = {"acme/widget-1": 128_000, "acme/widget-1-turbo": 8_000}
        found = lookup_window("acme/widget-1", windows)
        assert (found.tokens, found.exact) == (128_000, True)

    def test_prefix_match_takes_the_smallest(self):
        """A slug harvested from a prefix glob names a line, not a model. The
        ceiling check must not be argued out of a finding by an optimistic
        sibling, so the smallest window in the line is taken."""
        windows = {"acme/widget-9": 1_000_000, "acme/widget-1": 32_000}
        found = lookup_window("acme/widget", windows)
        assert (found.tokens, found.exact) == (32_000, False)
        assert found.matched_ids == ("acme/widget-1", "acme/widget-9")

    def test_prefix_match_requires_a_separator(self):
        assert lookup_window("acme/widget", {"acme/widgetry": 1}).tokens is None

    def test_gateway_prefix_is_normalised_away(self):
        windows = {"google/gemini-3.5-flash": 1_048_576}
        assert lookup_window("openrouter/google/gemini-3.5-flash", windows).tokens == 1_048_576

    def test_unknown_slug_degrades_to_none(self):
        assert lookup_window("acme/widget-1", {}).tokens is None
        assert lookup_window("acme/widget-1", {}).render() == "-"

    def test_context_windows_skips_malformed_entries(self):
        models = [
            {"id": "a/b", "context_length": 100},
            {"id": "c/d"},
            {"id": "e/f", "context_length": 0},
            {"context_length": 50},
        ]
        assert context_windows(models) == {"a/b": 100}


class TestOpenRouterFetch:
    def test_offline_with_no_cache_degrades_instead_of_raising(self, tmp_path: Path):
        models, source = load_openrouter_models(tmp_path / "absent.json", offline=True)
        assert models == []
        assert source.startswith("unavailable")

    def test_offline_uses_a_stale_cache(self, tmp_path: Path):
        cache = tmp_path / "or.json"
        cache.write_text(json.dumps({"data": [{"id": "a/b", "context_length": 1}]}))
        models, source = load_openrouter_models(cache, offline=True, refresh=True)
        assert models == [{"id": "a/b", "context_length": 1}]
        assert "cache" in source

    def test_fetch_failure_falls_back_to_cache(self, tmp_path: Path):
        cache = tmp_path / "or.json"
        cache.write_text(json.dumps({"data": [{"id": "a/b", "context_length": 1}]}))
        with patch(
            "scripts.analysis.audit_preset_fallthrough.fetch_openrouter_models",
            side_effect=OSError("boom"),
        ):
            models, source = load_openrouter_models(cache, refresh=True)
        assert models and "fetch failed" in source

    def test_corrupt_cache_is_ignored(self, tmp_path: Path):
        cache = tmp_path / "or.json"
        cache.write_text("{not json")
        models, source = load_openrouter_models(cache, offline=True)
        assert models == []
        assert source.startswith("unavailable")

    def test_successful_fetch_is_cached(self, tmp_path: Path):
        cache = tmp_path / "nested" / "or.json"
        payload = [{"id": "a/b", "context_length": 7}]
        with patch(
            "scripts.analysis.audit_preset_fallthrough.fetch_openrouter_models",
            return_value=payload,
        ):
            models, source = load_openrouter_models(cache, refresh=True)
        assert models == payload
        assert "live fetch" in source
        assert json.loads(cache.read_text())["data"] == payload


# ---------------------------------------------------------------------------
# Context-ceiling detection
# ---------------------------------------------------------------------------


def _ceilings(preset_block: dict, slugs: list[str], windows: dict[str, int]):
    with patch(
        "scripts.analysis.audit_preset_fallthrough.get_resolved_presets",
        return_value={"presets": {"p": preset_block}},
    ):
        return find_ceilings(slugs, "", windows)


class TestFindCeilings:
    def test_ceiling_above_the_smallest_matched_window_is_an_overshoot(self):
        """A ceiling picked for a 256K sibling silently kills the 128K one."""
        block = {
            "match": ["acme/widget*"],
            "max_context_tokens": 250_000,
            "context_watermark": 8_000,
        }
        found = _ceilings(
            block,
            ["acme/widget-big", "acme/widget-small"],
            {"acme/widget-big": 262_144, "acme/widget-small": 131_072},
        )
        assert len(found) == 1
        assert found[0].kind == "overshoot"
        assert found[0].smallest_slug == "acme/widget-small"
        assert found[0].ceiling == 258_000
        assert overshoots(found) == found

    def test_ceiling_well_below_the_window_is_an_undershoot(self):
        block = {
            "match": ["acme/widget*"],
            "max_context_tokens": 128_000,
            "context_watermark": 8_000,
        }
        found = _ceilings(block, ["acme/widget-1"], {"acme/widget-1": 1_048_576})
        assert [f.kind for f in found] == ["undershoot"]
        assert overshoots(found) == []

    def test_a_ceiling_that_fits_is_not_reported(self):
        block = {
            "match": ["acme/widget*"],
            "max_context_tokens": 120_000,
            "context_watermark": 8_000,
        }
        assert _ceilings(block, ["acme/widget-1"], {"acme/widget-1": 131_072}) == []

    def test_scope_is_glob_match_not_resolution(self):
        """A slug an earlier preset currently wins is still in scope: it becomes
        exposed the moment that earlier entry is narrowed or removed."""
        block = {
            "match": ["acme/widget*"],
            "max_context_tokens": 250_000,
            "context_watermark": 8_000,
        }
        with patch(
            "scripts.analysis.audit_preset_fallthrough.get_resolved_presets",
            return_value={
                "presets": {
                    "earlier": {"match": ["acme/widget-small"]},
                    "p": block,
                }
            },
        ):
            found = find_ceilings(["acme/widget-small"], "", {"acme/widget-small": 131_072})
        assert [f.preset for f in found] == ["p"]

    def test_preset_without_both_keys_is_skipped(self):
        block = {"match": ["acme/widget*"], "max_context_tokens": 250_000}
        assert _ceilings(block, ["acme/widget-1"], {"acme/widget-1": 1_000}) == []

    def test_no_window_data_means_no_finding(self):
        block = {
            "match": ["acme/widget*"],
            "max_context_tokens": 250_000,
            "context_watermark": 8_000,
        }
        assert _ceilings(block, ["acme/widget-1"], {}) == []

    def test_overshoots_sort_ahead_of_undershoots(self):
        findings = [
            CeilingFinding("u", "undershoot", 1, 1, 0, "s", 100, ()),
            CeilingFinding("o", "overshoot", 1, 1, 0, "s", 0, ()),
        ]
        findings.sort(key=lambda f: (f.kind != "overshoot", f.smallest_window - f.ceiling))
        assert [f.preset for f in findings] == ["o", "u"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_default_run_covers_the_sweep_lineup(self):
        slugs = default_slugs()
        for required in (
            "openai/gpt-5.6-sol",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-5",
            "moonshotai/kimi-k2.7-code",
            "moonshotai/kimi-k2.6",
            "moonshotai/kimi-k3",
            "google/gemini-3.1-pro-preview",
            "x-ai/grok-4",
            "deepseek/deepseek-v4",
            "z-ai/glm-5.1",
            "qwen/qwen3-coder",
        ):
            assert required in slugs
        assert len(slugs) == len(set(slugs))

    def test_json_output_is_parseable(self, tmp_path: Path, capsys):
        models_file = tmp_path / "slugs.txt"
        models_file.write_text("moonshotai/kimi-k3  # sibling\n\n# comment only\n")
        code = main(
            [
                "--models-file",
                str(models_file),
                "--json",
                "--offline",
                "--cache",
                str(tmp_path / "absent.json"),
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert [m["slug"] for m in payload["models"]] == ["moonshotai/kimi-k3"]
        assert payload["models"][0]["preset"] == "moonshot_kimi_k3"
        assert payload["models"][0]["openrouter_context_window"] is None

    def test_explicit_slugs_render_a_table(self, tmp_path: Path, capsys):
        """Every named slug gets a row carrying the preset it resolved to."""
        code = main(
            [
                "moonshotai/kimi-k2.7-code",
                "moonshotai/kimi-k3",
                "--offline",
                "--cache",
                str(tmp_path / "absent.json"),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        for slug in ("moonshotai/kimi-k2.7-code", "moonshotai/kimi-k3"):
            assert slug in out
            assert resolve_effective_preset(slug, "openrouter") in out

    def test_fail_on_suspect_exits_nonzero(self, tmp_path: Path, capsys):
        with _synthetic_fallthrough():
            code = main(
                [
                    "acme/thing-1",
                    "acme/thing-2",
                    "--offline",
                    "--fail-on-suspect",
                    "--cache",
                    str(tmp_path / "absent.json"),
                ]
            )
        out = capsys.readouterr().out
        assert "SUSPECT — shared multi-vendor preset, sibling carries more (1)" in out
        assert code == 1

    def test_clean_slug_set_exits_zero_under_fail_on_suspect(self, tmp_path: Path, capsys):
        code = main(
            [
                "moonshotai/kimi-k3",
                "--offline",
                "--fail-on-suspect",
                "--cache",
                str(tmp_path / "absent.json"),
            ]
        )
        capsys.readouterr()
        assert code == 0

    def test_audit_never_writes_preset_data(self, tmp_path: Path, capsys):
        """The audit is read-only by contract. ``get_resolved_presets`` hands
        out a defensive copy; this pins that the audit does not reach past it."""
        presets_path = (
            Path(__file__).resolve().parents[2]
            / "tolokaforge_models"
            / "src"
            / "tolokaforge_models"
            / "data"
            / "model_presets.yaml"
        )
        before = presets_path.read_bytes()
        main(["--offline", "--cache", str(tmp_path / "absent.json")])
        capsys.readouterr()
        assert presets_path.read_bytes() == before
