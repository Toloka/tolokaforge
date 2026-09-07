"""Unit tests for ``automation.cost_summary``: the three claude -p output shapes, the
aggregate walk, the key-usage deltas (kept in their own dir), the route caveat, what the
uploadable shapes may contain, and the two renderings. The HTTP snapshot is exercised
against a fake ``urlopen``; the workflow wiring is not."""

from __future__ import annotations

import io
import json
from pathlib import Path

import automation.cost_summary as cs
import pytest

pytestmark = pytest.mark.unit

_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 7,
    "duration_ms": 65_000,
    "total_cost_usd": 1.25,
    "result": "I edited the preset overlay. OPENROUTER_API_KEY=sk-or-v1-would-be-here",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 2_000,
        "cache_read_input_tokens": 90_000,
        "cache_creation_input_tokens": 3_000,
    },
}


class TestParseAgentResult:
    def test_a_single_result_object(self):
        assert cs.parse_agent_result(json.dumps(_RESULT))["num_turns"] == 7

    def test_the_verbose_array_yields_its_result_event(self):
        text = json.dumps([{"type": "system", "subtype": "init"}, {"type": "assistant"}, _RESULT])
        assert cs.parse_agent_result(text)["total_cost_usd"] == 1.25

    def test_stream_json_yields_the_last_result_line(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            "not json at all",
            json.dumps({"type": "assistant", "message": {}}),
            json.dumps({**_RESULT, "total_cost_usd": 0.5}),
        ]
        assert cs.parse_agent_result("\n".join(lines))["total_cost_usd"] == 0.5

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "garbage", json.dumps([{"type": "assistant"}]), json.dumps({"error": "x"})],
    )
    def test_nothing_usable_is_none(self, text):
        # Includes an object WITHOUT type: "result" - a `{"error": ...}` body is not a run.
        assert cs.parse_agent_result(text) is None


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)


def _snapshots(key_dir: Path) -> None:
    _write(key_dir / "key_start.json", {"at": "t0", "usage_usd": 100.0})
    _write(key_dir / "key_after_observe.json", {"at": "t1", "usage_usd": 104.0})
    _write(key_dir / "key_end.json", {"at": "t2", "usage_usd": 106.5})


def _obs_dir(tmp_path: Path, *, snapshots: bool = True, route: str | None = None) -> Path:
    obs = tmp_path / "observation"
    _write(obs / "manifest.json", {"candidate": {"name": "x/model"}, "pr": 12})
    _write(
        obs / "wire_probes_1" / "aggregate.json", {"total_cost_usd": 2.0, "total_prompt_tokens": 10}
    )
    _write(
        obs / "resolve" / "reprobe_1" / "wire_probes_2" / "aggregate.json",
        {"total_cost_usd": 0.5, "total_prompt_tokens": 5},
    )
    _write(obs / "resolve" / "agent_iter_1.jsonl", json.dumps(_RESULT))
    _write(obs / "resolve" / "agent_iter_2.jsonl", "no result here")
    _write(
        obs / "resolve" / "agent_finalize.jsonl", json.dumps({**_RESULT, "total_cost_usd": 0.75})
    )
    if snapshots:
        _snapshots(obs / "cost")
    if route:
        _write(obs / "cost" / "route.txt", route + "\n")
    return obs


class TestBuildSummary:
    def test_agents_wire_and_key_deltas_are_attributed(self, tmp_path):
        summary = cs.build_summary(_obs_dir(tmp_path), run_url="https://run")

        agents = summary["agents"]
        assert agents["runs"] == 3 and agents["runs_parsed"] == 2
        assert agents["total_cost_usd"] == 2.0  # 1.25 + 0.75; the unparsed run counts as 0
        assert agents["total_turns"] == 14
        assert agents["usage"]["cache_read_input_tokens"] == 180_000
        assert [i["iteration"] for i in agents["iterations"]] == [1, 2]
        assert summary["wire_probes"]["observe"]["total_cost_usd"] == 2.0
        assert summary["wire_probes"]["reprobe"]["total_cost_usd"] == 0.5
        deltas = summary["key_usage"]["deltas_usd"]
        assert deltas == {"observe": 4.0, "resolve_finalize": 2.5, "total": 6.5}
        assert summary["totals_usd"]["attributed"] == 4.5
        assert summary["totals_usd"]["measured_key_delta"] == 6.5
        assert summary["candidate"] == {"name": "x/model"}
        assert summary["route"] == "openrouter"
        assert summary["noteworthy"] is True
        assert any("no parseable result" in note for note in summary["notes"])

    def test_the_summary_never_carries_the_transcript_or_the_lifetime_usage(self, tmp_path):
        text = json.dumps(cs.build_summary(_obs_dir(tmp_path)))
        assert "sk-or-v1" not in text and "I edited" not in text
        assert "usage_usd" not in text  # only deltas + snapshot timestamps
        assert json.loads(text)["key_usage"]["snapshots"]["key_end"] == {"at": "t2"}

    def test_snapshots_live_in_their_own_dir(self, tmp_path):
        obs = _obs_dir(tmp_path, snapshots=False)
        key_dir = tmp_path / "runner-temp" / "cost"
        _snapshots(key_dir)
        summary = cs.build_summary(obs, key_dir=key_dir)
        assert summary["totals_usd"]["measured_key_delta"] == 6.5

    def test_missing_snapshots_leave_deltas_none_and_say_so(self, tmp_path):
        summary = cs.build_summary(_obs_dir(tmp_path, snapshots=False))
        assert summary["key_usage"]["deltas_usd"] == {
            "observe": None,
            "resolve_finalize": None,
            "total": None,
        }
        assert summary["totals_usd"]["measured_key_delta"] is None
        assert any("snapshot is missing" in note for note in summary["notes"])

    def test_iterations_sort_by_number_not_by_name(self, tmp_path):
        obs = tmp_path / "observation"
        for n in range(1, 13):
            _write(
                obs / "resolve" / f"agent_iter_{n}.jsonl", json.dumps({**_RESULT, "num_turns": n})
            )
        agents = cs.build_summary(obs)["agents"]
        assert [i["iteration"] for i in agents["iterations"]] == list(range(1, 13))
        assert [i["num_turns"] for i in agents["iterations"]] == list(range(1, 13))
        md = cs.render_markdown(cs.build_summary(obs))
        assert md.index("Resolve agent iter 9 ") < md.index("Resolve agent iter 10 ")
        assert "Resolve agent iter 12 (12 turns, success)" in md

    def test_the_gateway_route_is_named_and_the_observe_delta_is_not_a_probe_cost(self, tmp_path):
        summary = cs.build_summary(_obs_dir(tmp_path, route="litellm"))
        assert summary["route"] == "litellm"
        assert any("route=litellm" in note for note in summary["notes"])
        assert "agent only; probes billed the gateway key" in cs.render_line(summary)
        assert "| key delta | n/a (billed on the gateway key) |" in cs.render_markdown(summary)

    def test_an_observe_only_run_attributes_the_whole_delta_to_observe(self, tmp_path):
        """observe -> needs-human: no gateway step, so no after-observe snapshot and no agent."""
        obs = tmp_path / "observation"
        _write(obs / "wire_probes_1" / "aggregate.json", {"total_cost_usd": 2.0})
        _write(obs / "cost" / "key_start.json", {"at": "t0", "usage_usd": 100.0})
        _write(obs / "cost" / "key_end.json", {"at": "t2", "usage_usd": 107.0})
        summary = cs.build_summary(obs)
        assert summary["key_usage"]["deltas_usd"] == {
            "observe": 7.0,
            "resolve_finalize": None,
            "total": 7.0,
        }
        assert any("resolve did not run" in note for note in summary["notes"])
        assert "| key delta | \\$7.0000 |" in cs.render_markdown(summary)

    def test_unpriced_wire_runs_are_counted_and_said(self, tmp_path):
        obs = tmp_path / "observation"
        _write(obs / "wire_probes_1" / "aggregate.json", {"total_cost_usd": None})
        _write(obs / "wire_probes_2" / "aggregate.json", {"total_cost_usd": 1.0})
        summary = cs.build_summary(obs)
        assert summary["wire_probes"]["observe"] == {
            "runs": 2,
            "unpriced_runs": 1,
            "total_cost_usd": 1.0,
            "tokens": {"prompt": 0, "completion": 0, "cached": 0},
        }
        assert any("1 wire run(s) carry no cost" in note for note in summary["notes"])

    def test_a_run_that_ran_agents_is_noteworthy_even_with_no_figures(self, tmp_path):
        obs = tmp_path / "observation"
        _write(obs / "resolve" / "agent_iter_1.jsonl", "cut off before any result event")
        assert cs.build_summary(obs)["noteworthy"] is True

    def test_an_empty_observation_dir_is_all_zeros_and_not_noteworthy(self, tmp_path):
        summary = cs.build_summary(tmp_path)
        assert summary["agents"]["runs"] == 0
        assert summary["totals_usd"]["attributed"] == 0.0
        assert summary["noteworthy"] is False

    def test_a_download_with_only_normalized_events_still_has_the_agents(self, tmp_path):
        obs = tmp_path / "observation"
        _write(
            obs / "cost" / "agent_iter_3.json",
            cs.normalize_agent_result(_RESULT, file="agent_iter_3.jsonl", iteration=3),
        )
        agents = cs.build_summary(obs)["agents"]
        assert agents["runs"] == 1 and agents["total_cost_usd"] == 1.25
        assert agents["iterations"][0]["iteration"] == 3


class TestRendering:
    def test_the_line_leads_with_the_billed_figure_when_known(self, tmp_path):
        line = cs.render_line(cs.build_summary(_obs_dir(tmp_path)))
        assert line.startswith("Cost: $6.5000 billed on the automation key - agents $2.0000")
        assert "over 14 turns in 3 run(s), wire probes $2.5000" in line
        assert "\n" not in line

    def test_the_line_falls_back_to_the_attributed_lower_bound(self, tmp_path):
        line = cs.render_line(cs.build_summary(_obs_dir(tmp_path, snapshots=False)))
        assert line.startswith("Cost: >= $4.5000 attributed (key delta unavailable)")

    def test_the_markdown_has_one_row_per_stage_escapes_dollars_and_lists_the_notes(self, tmp_path):
        md = cs.render_markdown(cs.build_summary(_obs_dir(tmp_path), run_url="https://run"))
        assert md.startswith("### Cost summary - `x/model`")
        assert "| Resolve agent iter 1 (7 turns, success) | CLI self-report | \\$1.2500 |" in md
        assert "| Resolve agent iter 2 (? turns, unparsed) | CLI self-report | n/a |" in md
        assert "| Finalize agent (7 turns, success) | CLI self-report | \\$0.7500 |" in md
        assert "| **Total billed** (key delta) | | **\\$6.5000** |" in md
        assert "cache read 180,000" in md
        assert "candidate's wire calls only" in md
        assert "[Run](https://run)" in md
        # No unescaped dollar anywhere: GitHub would render `$a ... $b` as math.
        assert "$" not in md.replace("\\$", "")

    def test_the_digest_line_names_how_the_run_ended(self):
        assert cs.digest_line(None, label="a.jsonl").startswith("a.jsonl: no result event")
        line = cs.digest_line({**_RESULT, "subtype": "error_max_turns"}, label="b.jsonl")
        assert "subtype=error_max_turns" in line and "turns=7" in line and "cost=$1.2500" in line


class TestKeySnapshot:
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def test_it_writes_the_usage_figure_and_nothing_that_identifies_the_key(
        self, tmp_path, monkeypatch
    ):
        payload = {
            "data": {
                "label": "sk-or-v1-abc...def",
                "usage": 1891.41,
                "usage_daily": 0,
                "limit": 750,
                "limit_remaining": 750,
            }
        }
        monkeypatch.setattr(
            cs.urllib.request,
            "urlopen",
            lambda request, timeout: self._Response(json.dumps(payload).encode()),
        )
        out = tmp_path / "cost" / "key_start.json"
        assert cs.snapshot_key(out, api_key="k") is True
        written = json.loads(out.read_text())
        assert set(written) == {"at", "usage_usd"} and written["usage_usd"] == 1891.41
        assert cs.read_key_snapshot(out)["usage_usd"] == 1891.41

    def test_no_key_means_no_file_and_no_failure(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        out = tmp_path / "key.json"
        assert cs.snapshot_key(out) is False
        assert not out.exists()
        assert cs.run_key_snapshot(str(out)) == 0

    def test_a_network_error_and_a_bad_payload_are_swallowed(self, tmp_path, monkeypatch):
        def boom(request, timeout):
            raise OSError("down")

        monkeypatch.setattr(cs.urllib.request, "urlopen", boom)
        assert cs.snapshot_key(tmp_path / "key.json", api_key="k") is False
        monkeypatch.setattr(
            cs.urllib.request,
            "urlopen",
            lambda request, timeout: self._Response(json.dumps({"data": {}}).encode()),
        )
        assert cs.snapshot_key(tmp_path / "key.json", api_key="k") is False


def test_run_writes_json_markdown_and_line(tmp_path, capsys):
    obs = _obs_dir(tmp_path, snapshots=False)
    key_dir = tmp_path / "runner-temp" / "cost"
    _snapshots(key_dir)
    code = cs.run(
        str(obs),
        out=str(obs / "cost" / "cost_summary.json"),
        md_out=str(obs / "cost" / "cost_summary.md"),
        line_out=str(obs / "cost" / "cost_summary.txt"),
        run_url="https://run",
        key_dir=str(key_dir),
    )
    assert code == 0
    assert json.loads((obs / "cost" / "cost_summary.json").read_text())["schema_version"] == 1
    assert (obs / "cost" / "cost_summary.md").read_text().startswith("### Cost summary")
    assert (obs / "cost" / "cost_summary.txt").read_text().startswith("Cost: $6.5000")
    assert capsys.readouterr().out.startswith("Cost: $6.5000")


def test_run_writes_only_the_json_when_nothing_was_spent(tmp_path, capsys):
    obs = tmp_path / "observation"
    obs.mkdir()
    cs.run(
        str(obs),
        out=str(obs / "cost" / "cost_summary.json"),
        md_out=str(obs / "cost" / "cost_summary.md"),
        line_out=str(obs / "cost" / "cost_summary.txt"),
    )
    assert (obs / "cost" / "cost_summary.json").exists()
    assert not (obs / "cost" / "cost_summary.md").exists()
    assert not (obs / "cost" / "cost_summary.txt").exists()
    assert "nothing spent on record" in capsys.readouterr().out


def test_run_digest_writes_the_normalized_event_without_the_transcript(tmp_path, capsys):
    raw = tmp_path / "resolve" / "agent_iter_4.jsonl"
    _write(raw, json.dumps({"type": "system"}) + "\n" + json.dumps(_RESULT))
    out = tmp_path / "cost" / "agent_iter_4.json"
    assert cs.run_digest(str(raw), out=str(out)) == 0
    assert capsys.readouterr().out.startswith("agent_iter_4.jsonl: subtype=success")
    normalized = json.loads(out.read_text())
    assert normalized["iteration"] == 4 and normalized["total_cost_usd"] == 1.25
    assert "result" not in normalized and "sk-or-v1" not in out.read_text()
