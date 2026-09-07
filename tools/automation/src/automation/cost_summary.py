"""Cost accounting for one auto-integration run (``automation cost-summary``).

Three sources, three different truths, kept apart on purpose:

* **agents** - the resolve / finalize ``claude -p`` runs report their own spend
  (``total_cost_usd`` in the CLI's ``result`` event). That is the Anthropic list price for the
  alias; on the gateway route cache WRITES are not surfaced, so it is a lower bound.
* **wire probes** - the engine's own ``aggregate.json`` under every ``wire_probes_*`` run
  (observe) and ``resolve/reprobe_*`` (reprobe). Exact for the candidate's wire calls; the
  capability + variant probes and the user simulator leave no cost artifact at all.
* **key usage** - snapshots of the automation key's lifetime usage (OpenRouter
  ``GET /api/v1/key``) at run start, after observe and at the end. Their deltas are the BILLED
  figure and the only one that also covers the probes without an artifact. Key-level, so a
  concurrent run on the same key is included; on the ``litellm`` route the probes bill the
  gateway key instead, and the deltas cover the agent alone. The summary says both.

What may leave the runner: the summary and the NORMALIZED agent result events (cost, turns,
usage, how the run ended - never the transcript, which can echo the agent's environment). The
key snapshots carry the key's lifetime usage figure, so the workflow keeps them outside the
observation dir and only their deltas reach the summary.

See docs/AUTO_INTEGRATION.md § Cost summary.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tolokaforge.secrets import EnvProvider, SecretManager

KEY_USAGE_URL = "https://openrouter.ai/api/v1/key"

#: Under the observation dir: the effective route marker, the normalized agent result events
#: and the rendered summary - everything in here is safe to upload.
COST_DIR = "cost"
ROUTE_FILE = "route.txt"
DEFAULT_ROUTE = "openrouter"
GATEWAY_ROUTE = "litellm"
KEY_SNAPSHOTS = ("key_start", "key_after_observe", "key_end")

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_ITER_RE = re.compile(r"^agent_iter_(\d+)\.")


def _log(message: str) -> None:
    print(f"cost-summary: {message}", file=sys.stderr)


def _usd(value: float | None, *, escape: bool = False) -> str:
    if value is None:
        return "n/a"
    # GitHub renders `$...$` on one line as math; the markdown body escapes its dollars.
    return ("\\$" if escape else "$") + f"{value:,.4f}"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Agent results (claude -p output)
# ---------------------------------------------------------------------------


def parse_agent_result(text: str) -> dict[str, Any] | None:
    """The ``result`` event of a ``claude -p`` run, whatever output format produced *text*.

    ``--output-format json`` prints one object; with ``--verbose`` it prints an array of every
    message; ``stream-json`` prints one JSON object per line. All three end in a
    ``{"type": "result", ...}`` object, which is the one carrying the totals. ``None`` when no
    such object is found (an agent that never got to answer, or a file that is not JSON).
    """
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed if parsed.get("type") == "result" else None
    if isinstance(parsed, list):
        results = [
            item for item in parsed if isinstance(item, dict) and item.get("type") == "result"
        ]
        return results[-1] if results else None
    # JSON lines: keep the last result event, ignore anything that does not parse.
    last: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("type") == "result":
            last = item
    return last


def iteration_number(path: Path) -> int | None:
    """The resolve iteration a result file belongs to, from its name (``agent_iter_<n>.*``)."""
    match = _ITER_RE.match(path.name)
    return int(match.group(1)) if match else None


def normalize_agent_result(
    result: dict[str, Any] | None, *, file: str, iteration: int | None = None
) -> dict[str, Any]:
    """The subset of a result event the summary keeps - cost, turns, usage, how the run ended.

    Deliberately NOT the ``result`` text or anything else from the transcript: this is the
    shape that gets uploaded.
    """
    usage_raw = result.get("usage") if isinstance(result, dict) else None
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    return {
        "file": file,
        "iteration": iteration,
        "parsed": result is not None,
        "total_cost_usd": round(_num(result.get("total_cost_usd")), 6) if result else None,
        "num_turns": int(_num(result.get("num_turns"))) if result else None,
        "duration_ms": int(_num(result.get("duration_ms"))) if result else None,
        "subtype": str(result.get("subtype") or "") if result else None,
        "is_error": bool(result.get("is_error")) if result else None,
        "usage": {field: int(_num(usage.get(field))) for field in _USAGE_FIELDS},
    }


def _is_normalized(data: Any) -> bool:
    return isinstance(data, dict) and {"file", "parsed", "usage"} <= set(data)


def read_agent_result(path: Path) -> dict[str, Any]:
    """A raw ``claude -p`` output file OR an already-normalized one (the uploaded shape)."""
    try:
        text = path.read_text()
    except OSError:
        text = ""
    try:
        data = json.loads(text) if text.strip() else None
    except ValueError:
        data = None
    if _is_normalized(data):
        return data
    return normalize_agent_result(
        parse_agent_result(text), file=path.name, iteration=iteration_number(path)
    )


def _agent_files(obs_dir: Path) -> tuple[list[Path], Path | None]:
    # The raw files live under resolve/ on the runner; the normalized copies under cost/ are
    # what an artifact holds, so a summary rebuilt from a download still finds the agents.
    for directory in (obs_dir / "resolve", obs_dir / COST_DIR):
        iterations = sorted(
            (
                p
                for p in directory.glob("agent_iter_*.json*")
                if p.is_file() and iteration_number(p) is not None
            ),
            key=lambda p: iteration_number(p) or 0,
        )
        finalize = next((p for p in directory.glob("agent_finalize.json*") if p.is_file()), None)
        if iterations or finalize:
            return iterations, finalize
    return [], None


def agent_costs(obs_dir: Path) -> dict[str, Any]:
    iterations, finalize = _agent_files(obs_dir)
    iteration_results = [read_agent_result(p) for p in iterations]
    finalize_result = read_agent_result(finalize) if finalize else None
    all_results = iteration_results + ([finalize_result] if finalize_result else [])
    parsed = [r for r in all_results if r["parsed"]]
    return {
        "iterations": iteration_results,
        "finalize": finalize_result,
        "runs": len(all_results),
        "runs_parsed": len(parsed),
        "total_cost_usd": round(sum(_num(r["total_cost_usd"]) for r in parsed), 6),
        "total_turns": sum(int(r["num_turns"] or 0) for r in parsed),
        "usage": {field: sum(int(r["usage"][field]) for r in parsed) for field in _USAGE_FIELDS},
    }


# ---------------------------------------------------------------------------
# Wire probes (engine aggregate.json)
# ---------------------------------------------------------------------------


def _aggregate_costs(paths: list[str]) -> dict[str, Any]:
    total = 0.0
    tokens = {"prompt": 0, "completion": 0, "cached": 0}
    runs = 0
    unpriced = 0
    for path in paths:
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        runs += 1
        # An engine with no pricing for the model writes null, not 0: unknown, and said so.
        if data.get("total_cost_usd") is None:
            unpriced += 1
        total += _num(data.get("total_cost_usd"))
        tokens["prompt"] += int(_num(data.get("total_prompt_tokens")))
        tokens["completion"] += int(_num(data.get("total_completion_tokens")))
        tokens["cached"] += int(_num(data.get("total_cached_tokens")))
    return {
        "runs": runs,
        "unpriced_runs": unpriced,
        "total_cost_usd": round(total, 6),
        "tokens": tokens,
    }


def wire_costs(obs_dir: Path) -> dict[str, Any]:
    # Same discovery as observe._wire_findings: the observe stage writes one wire_probes_* run
    # directly under the obs dir, a reprobe writes its own under resolve/reprobe_<i>/.
    observe = _aggregate_costs(sorted(glob.glob(str(obs_dir / "wire_probes_*" / "aggregate.json"))))
    reprobe = _aggregate_costs(
        sorted(
            glob.glob(str(obs_dir / "resolve" / "reprobe_*" / "wire_probes_*" / "aggregate.json"))
        )
    )
    return {
        "observe": observe,
        "reprobe": reprobe,
        "unpriced_runs": observe["unpriced_runs"] + reprobe["unpriced_runs"],
        "total_cost_usd": round(observe["total_cost_usd"] + reprobe["total_cost_usd"], 6),
    }


# ---------------------------------------------------------------------------
# Route marker + key usage snapshots (OpenRouter GET /api/v1/key)
# ---------------------------------------------------------------------------


def read_route(obs_dir: Path) -> str:
    """The route the workflow's .env step actually configured (``openrouter`` when unrecorded)."""
    try:
        route = (obs_dir / COST_DIR / ROUTE_FILE).read_text().strip().lower()
    except OSError:
        return DEFAULT_ROUTE
    return route or DEFAULT_ROUTE


def snapshot_key(out: Path, *, api_key: str | None = None, timeout: float = 20.0) -> bool:
    """Write the key's lifetime usage to *out* - the one number a delta needs. False on any miss.

    The figure is account-key-level, and it is the key's LIFETIME spend, which is why the
    workflow keeps these files out of every artifact.
    """
    if api_key is None:
        # Env-only, like gateway_catalog: the workflow maps the secret in per step, and dotenv
        # precedence would let a developer's local .env answer a CI run.
        api_key = SecretManager([EnvProvider()]).get_secret("OPENROUTER_API_KEY")
    if not api_key:
        _log("no OPENROUTER_API_KEY in the environment; skipping the key snapshot")
        return False
    request = urllib.request.Request(KEY_USAGE_URL, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log(f"key snapshot failed: {exc}")
        return False
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("usage") is None:
        _log("key snapshot: unexpected payload (no data.usage)")
        return False
    snapshot = {
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "usage_usd": _num(data.get("usage")),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2) + "\n")
    return True


def read_key_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "usage_usd" in data else None


def key_usage(key_dir: Path) -> dict[str, Any]:
    """Deltas between the snapshots in *key_dir*; the absolute figures stay on the runner."""
    snaps = {name: read_key_snapshot(key_dir / f"{name}.json") for name in KEY_SNAPSHOTS}

    def delta(a: str, b: str) -> float | None:
        if snaps[a] is None or snaps[b] is None:
            return None
        return round(_num(snaps[b]["usage_usd"]) - _num(snaps[a]["usage_usd"]), 6)

    return {
        "snapshots": {
            name: ({"at": snap.get("at")} if snap is not None else None)
            for name, snap in snaps.items()
        },
        "deltas_usd": {
            "observe": delta("key_start", "key_after_observe"),
            "resolve_finalize": delta("key_after_observe", "key_end"),
            "total": delta("key_start", "key_end"),
        },
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def build_summary(
    obs_dir: Path, *, run_url: str | None = None, key_dir: Path | None = None
) -> dict[str, Any]:
    manifest_path = obs_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    except (OSError, ValueError):
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    route = read_route(obs_dir)
    agents = agent_costs(obs_dir)
    wire = wire_costs(obs_dir)
    usage = key_usage(key_dir if key_dir is not None else obs_dir / COST_DIR)
    attributed = round(agents["total_cost_usd"] + wire["total_cost_usd"], 6)
    deltas = usage["deltas_usd"]
    measured = deltas["total"]
    # The after-observe snapshot is taken by the gateway step, which runs only when the gate
    # chains to resolve. On an observe -> needs-human run it is missing while start + end are
    # there, and no agent ran: the whole delta IS the observe spend, so say so instead of n/a.
    observe_is_total = deltas["observe"] is None and measured is not None and agents["runs"] == 0
    if observe_is_total:
        deltas["observe"] = measured

    notes = [
        "agent cost is the Claude Code CLI's own report at Anthropic list price; cache writes "
        "are not surfaced on the gateway route, so it is a lower bound",
        "the aggregates cover the candidate's wire calls only; the capability + variant probes "
        "and the user simulator's calls leave no cost artifact, so the attributed total is a "
        "lower bound of the run's spend",
        "key deltas are account-key-level (ARENA_AUTOMATION_OPENROUTER_API_KEY) and include "
        "any run that shared the key in the same window",
    ]
    if route == GATEWAY_ROUTE:
        notes.append(
            "route=litellm: the observe + reprobe probes (candidate and user simulator) were "
            "billed on the gateway key, not on this one, so the key deltas cover the agent alone "
            "and the observe delta is not a probe cost"
        )
    if measured is None:
        notes.append("key usage deltas unavailable (a snapshot is missing)")
    if observe_is_total:
        notes.append(
            "resolve did not run (the after-observe snapshot exists only when the gate chains), "
            "so the whole key delta is the observe spend"
        )
    if wire["unpriced_runs"]:
        notes.append(
            f"{wire['unpriced_runs']} wire run(s) carry no cost (the engine had no pricing for "
            "the model); they count as zero here"
        )
    if agents["runs"] and agents["runs_parsed"] < agents["runs"]:
        notes.append(
            f"{agents['runs'] - agents['runs_parsed']} agent run(s) produced no parseable result "
            "event and count as zero here"
        )

    return {
        "schema_version": 1,
        "candidate": manifest.get("candidate"),
        "pr": manifest.get("pr"),
        "run_url": run_url,
        "route": route,
        "agents": agents,
        "wire_probes": wire,
        "key_usage": usage,
        "totals_usd": {
            "attributed": attributed,
            "agents": agents["total_cost_usd"],
            "wire_probes": wire["total_cost_usd"],
            "measured_key_delta": measured,
        },
        # A run with nothing on record (parse failed early, or every source is missing) has
        # nothing worth a PR comment or a Slack line; the JSON still records that. A run that
        # demonstrably ran agents or probes is reported even when every figure came back empty.
        "noteworthy": (
            attributed > 0 or bool(measured) or agents["runs"] > 0 or wire["observe"]["runs"] > 0
        ),
        "notes": notes,
    }


def render_line(summary: dict[str, Any], *, escape: bool = False) -> str:
    """One line for Slack: the billed figure first when it exists, the attributed one otherwise."""
    totals = summary["totals_usd"]
    agents = summary["agents"]
    measured = totals["measured_key_delta"]

    def usd(value: float | None) -> str:
        return _usd(value, escape=escape)

    if measured is None:
        headline = f"Cost: >= {usd(totals['attributed'])} attributed (key delta unavailable)"
    elif summary.get("route") == GATEWAY_ROUTE:
        headline = (
            f"Cost: {usd(measured)} billed on the automation key (agent only; probes billed the "
            "gateway key)"
        )
    else:
        headline = f"Cost: {usd(measured)} billed on the automation key"
    return (
        f"{headline} - agents {usd(totals['agents'])} over {agents['total_turns']} turns in "
        f"{agents['runs']} run(s), wire probes {usd(totals['wire_probes'])}"
    )


def render_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals_usd"]
    agents = summary["agents"]
    wire = summary["wire_probes"]
    deltas = summary["key_usage"]["deltas_usd"]
    gateway = summary.get("route") == GATEWAY_ROUTE
    candidate = summary.get("candidate") or {}
    name = candidate.get("name") if isinstance(candidate, dict) else None

    def usd(value: float | None) -> str:
        return _usd(value, escape=True)

    def agent_row(label: str, item: dict[str, Any]) -> str:
        turns = item["num_turns"] if item["parsed"] else "?"
        return (
            f"| {label} ({turns} turns, {item['subtype'] or 'unparsed'}) | CLI self-report | "
            f"{usd(item['total_cost_usd'])} |"
        )

    lines = [
        "### Cost summary" + (f" - `{name}`" if name else ""),
        "",
        render_line(summary, escape=True),
        "",
        "| Stage | Source | Cost |",
        "|---|---|---|",
        f"| Observe: wire probes ({wire['observe']['runs']} run) | aggregate.json | "
        f"{usd(wire['observe']['total_cost_usd'])} |",
        "| Observe: all probes incl. capability + variants | key delta | "
        + ("n/a (billed on the gateway key)" if gateway else usd(deltas["observe"]))
        + " |",
    ]
    for item in agents["iterations"]:
        label = f"Resolve agent iter {item['iteration'] if item['iteration'] is not None else '?'}"
        lines.append(agent_row(label, item))
    if wire["reprobe"]["runs"]:
        lines.append(
            f"| Resolve: reprobe wire ({wire['reprobe']['runs']} run) | aggregate.json | "
            f"{usd(wire['reprobe']['total_cost_usd'])} |"
        )
    if agents["finalize"]:
        lines.append(agent_row("Finalize agent", agents["finalize"]))
    lines += [
        f"| Resolve + finalize, everything on this key | key delta | "
        f"{usd(deltas['resolve_finalize'])} |",
        f"| **Total attributed** (agents + wire) | | **{usd(totals['attributed'])}** |",
        f"| **Total billed** (key delta) | | **{usd(totals['measured_key_delta'])}** |",
        "",
    ]
    usage = agents["usage"]
    lines.append(
        f"Agent tokens: {usage['input_tokens']:,} in / {usage['output_tokens']:,} out, "
        f"cache read {usage['cache_read_input_tokens']:,}, "
        f"cache write {usage['cache_creation_input_tokens']:,}."
    )
    lines += ["", *(f"- {note}" for note in summary["notes"])]
    if summary.get("run_url"):
        lines += ["", f"[Run]({summary['run_url']})"]
    return "\n".join(lines) + "\n"


def digest_line(result: dict[str, Any] | None, *, label: str) -> str:
    """One log line per agent run, so the job log still says how a run ended and what it cost."""
    if result is None:
        return f"{label}: no result event (agent produced no parseable output)"
    return (
        f"{label}: subtype={result.get('subtype') or '?'} is_error={bool(result.get('is_error'))} "
        f"turns={int(_num(result.get('num_turns')))} "
        f"duration={int(_num(result.get('duration_ms'))) // 1000}s "
        f"cost={_usd(_num(result.get('total_cost_usd')))}"
    )


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def run(
    obs_dir: str,
    *,
    out: str,
    md_out: str | None = None,
    line_out: str | None = None,
    run_url: str | None = None,
    key_dir: str | None = None,
) -> int:
    summary = build_summary(
        Path(obs_dir), run_url=run_url, key_dir=Path(key_dir) if key_dir else None
    )
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n")
    if not summary["noteworthy"]:
        # The workflow posts whatever md/line file exists, so not writing them IS the skip.
        print("cost-summary: nothing spent on record; no PR comment / Slack line written")
        return 0
    line = render_line(summary)
    if md_out:
        Path(md_out).write_text(render_markdown(summary))
    if line_out:
        Path(line_out).write_text(line + "\n")
    print(line)
    return 0


def run_key_snapshot(out: str) -> int:
    # Never a failing exit: accounting must not be able to fail the integration.
    snapshot_key(Path(out))
    return 0


def run_digest(path: str, *, out: str | None = None) -> int:
    file = Path(path)
    try:
        text = file.read_text()
    except OSError:
        text = ""
    result = parse_agent_result(text)
    print(digest_line(result, label=file.name))
    if out:
        # The uploadable shape: cost, turns, usage, ending - no transcript.
        normalized = normalize_agent_result(
            result, file=file.name, iteration=iteration_number(file)
        )
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(normalized, indent=2) + "\n")
    return 0
