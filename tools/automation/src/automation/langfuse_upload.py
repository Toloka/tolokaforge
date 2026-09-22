"""Send a pipeline agent's transcripts to the tracing receiver.

The agents this repository runs in CI (the resolve loop and the finalize step of a model
integration) write their Claude Code output to the runner and nowhere else: it is never an
artifact, because a tool result can carry the run's credentials. This is the one step that takes
those files off the runner, and it is deliberately narrow.

What it does, in order, per file:

1. **read** the output through :mod:`tolokaforge_langfuse.transcripts`, whose allowlist refuses a
   shape it does not know rather than guessing at it;
2. **gate** it: the tool input/output policy (``drop`` by default, so what a tool returned never
   leaves the runner at all);
3. **project** it to ingestion bodies and then to OTLP spans, the same two steps the trial path
   takes, so a transcript and a trial read alike in the same UI;
4. **scan** the serialised payload against the sentinel, which knows the credential shapes *and*
   the values this very process holds. A hit sends nothing;
5. **export** one batch per transcript.

Nothing here fails the pipeline on its own: the command reports what it refused, what it blocked
and what it could not send, and the workflow step carries ``continue-on-error``. Credentials are
read from the step's own environment, never logged, and never written to the receipt.

The wheel is an optional dependency (``automation[otel]``); it is imported inside the functions so
the rest of this tool loads without it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OTLP_PATH = "/api/public/otel/v1/traces"
PROJECTS_PATH = "/api/public/projects"
DEFAULT_RUN_TAG = "v1"
PROJECT_TIMEOUT_S = 10.0

# ``agent_iter_3.jsonl`` is the third resolve iteration; ``agent_finalize.jsonl`` is the finalize
# step. Anything else keeps its own stem, so a new agent step needs no change here to be traced.
_ITERATION = re.compile(r"^agent_iter_(\d+)$")


class UploadError(RuntimeError):
    """The upload cannot start: no receiver, no credentials, or the wheel is not installed."""


@dataclass(frozen=True)
class Receiver:
    """Where the spans go and what admits them. The headers never reach a log or a receipt."""

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    base_url: str | None = None

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> Receiver:
        env = env if env is not None else os.environ
        base = (env.get("LANGFUSE_BASE_URL") or "").rstrip("/")
        endpoint = env.get("LANGFUSE_OTLP_ENDPOINT") or (f"{base}{OTLP_PATH}" if base else "")
        if not endpoint:
            raise UploadError("no receiver: set LANGFUSE_BASE_URL or LANGFUSE_OTLP_ENDPOINT")
        public, secret = env.get("LANGFUSE_PUBLIC_KEY"), env.get("LANGFUSE_SECRET_KEY")
        if not public or not secret:
            raise UploadError("no credentials: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        token = b64encode(f"{public}:{secret}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        headers.update(_extra_headers(env.get("LANGFUSE_EXTRA_HEADERS")))
        return cls(endpoint=endpoint, headers=headers, base_url=base or None)

    def project_name(self) -> str | None:
        """The receiver's own name for the project these keys open, or ``None`` when it cannot be
        asked (an alias in front of the receiver may not route the REST API at all)."""
        if not self.base_url:
            return None
        request = urllib.request.Request(f"{self.base_url}{PROJECTS_PATH}", headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=PROJECT_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
        projects = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(projects, Sequence) and projects:
            first = projects[0]
            if isinstance(first, Mapping):
                return str(first.get("name") or "") or None
        return None


def _extra_headers(raw: str | None) -> dict[str, str]:
    """Headers a network admission layer in front of the receiver needs, as a JSON object. The
    values are credentials, so a malformed value is an error rather than a warning that scrolls."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise UploadError(f"LANGFUSE_EXTRA_HEADERS is not JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise UploadError("LANGFUSE_EXTRA_HEADERS must be a JSON object of header names to values")
    return {str(name): str(value) for name, value in parsed.items()}


def transcript_id_for(path: Path) -> str:
    """The id a transcript file takes in the trace id's task position."""
    stem = path.stem
    match = _ITERATION.match(stem)
    if match:
        return f"resolve/{int(match.group(1))}"
    if stem == "agent_finalize":
        return "finalize"
    return stem


@dataclass
class UploadReport:
    """What happened, in a shape a job summary and a receipt can both read."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not (self.refused or self.blocked or self.failed)

    @property
    def spans(self) -> int:
        return sum(int(entry["spans"]) for entry in self.sent)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "transcripts": len(self.sent),
            "spans": self.spans,
            "sent": self.sent,
            "refused": self.refused,
            "blocked": self.blocked,
            "failed": self.failed,
            "ok": self.ok,
        }

    def as_markdown(self) -> str:
        lines = [
            "### Agent transcripts",
            "",
            f"- sent: **{len(self.sent)}** transcript(s), {self.spans} span(s)"
            + (" (dry run, nothing left the runner)" if self.dry_run else ""),
        ]
        for label, entries in (
            ("refused by the reader", self.refused),
            ("blocked by the safety gate", self.blocked),
            ("not sent", self.failed),
        ):
            if entries:
                lines.append(f"- {label}: **{len(entries)}**")
                lines.extend(f"  - `{e['file']}`: {e['reason']}" for e in entries)
        return "\n".join(lines) + "\n"


def upload(
    directory: Path,
    *,
    run_id: str,
    label: str,
    session: str | None = None,
    run_tag: str = DEFAULT_RUN_TAG,
    environment: str | None = None,
    project: str | None = None,
    caller_tags: Mapping[str, str],
    metadata: Mapping[str, Any] | None = None,
    tool_io: str | None = None,
    producer_version: str = "automation",
    receiver: Receiver | None = None,
    dry_run: bool = False,
) -> UploadReport:
    """Read, gate, project and send every agent transcript under ``directory``."""
    try:
        from tolokaforge_langfuse import otlp_spans, otlp_transport, safety
        from tolokaforge_langfuse import transcripts as tr
    except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
        raise UploadError(
            "the tolokaforge-langfuse wheel is not installed; install automation[otel]"
        ) from exc
    from tolokaforge.observability import ids as engine_ids

    files = tr.transcript_files(Path(directory))
    report = UploadReport(dry_run=dry_run)
    if not files:
        return report

    if receiver is None and not dry_run:
        receiver = Receiver.from_environment()
    verified = project is not None and receiver is not None and receiver.project_name() == project
    gate = safety.SafetyGate.from_environment()
    contract = tr.id_contract(engine_ids)
    exporter = (
        otlp_transport.make_otlp_exporter(receiver.endpoint, receiver.headers, retry=False)
        if receiver is not None and not dry_run
        else None
    )

    for path in files:
        name = path.name
        try:
            transcript = tr.read_claude_output(path, transcript_id=transcript_id_for(path))
            gated = tr.redact(transcript, policy=tool_io or tr.TOOL_IO_DROP)
            options = tr.TranscriptOptions(
                run_tag=run_tag,
                run_id=run_id,
                label=label,
                session=session or run_id,
                environment=environment,
                producer_version=producer_version,
                project=project or tr.NONE,
                project_verified=verified,
                caller_tags=dict(caller_tags),
                metadata=dict(metadata or {}),
            )
            built = tr.build_events(gated, options, ids=contract)
        except tr.TranscriptError as exc:
            report.refused.append({"file": name, "reason": str(exc)})
            continue

        payload = json.dumps(built.events, default=str).encode("utf-8")
        findings = gate.scan(payload, what=name)
        if findings:
            # the finding names the rule and a masked excerpt; the value never reaches the receipt
            report.blocked.append({"file": name, "reason": ", ".join(str(f) for f in findings)})
            continue

        spans = otlp_spans.spans_from_events(built.events, environment=environment)
        if exporter is None:
            report.sent.append(
                {
                    "file": name,
                    "transcript_id": gated.transcript_id,
                    "trace_id": built.trace_id,
                    "spans": len(spans),
                }
            )
            continue
        try:
            outcome = exporter.export(spans)
        except Exception as exc:  # the receiver is not this pipeline's business to fail on
            report.failed.append({"file": name, "reason": f"export raised: {exc}"})
            continue
        if getattr(outcome, "name", str(outcome)) != "SUCCESS":
            report.failed.append({"file": name, "reason": f"export returned {outcome}"})
            continue
        report.sent.append(
            {
                "file": name,
                "transcript_id": gated.transcript_id,
                "trace_id": built.trace_id,
                "spans": len(spans),
            }
        )
    return report


def write_summary(report: UploadReport, path: str | None = None) -> None:
    """Append the report to the job summary when the runner offers one."""
    target = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(report.as_markdown())


def parse_pairs(values: Sequence[str] | None, separator: str, what: str) -> dict[str, str]:
    """``name<sep>value`` pairs from repeated command-line options."""
    pairs: dict[str, str] = {}
    for value in values or ():
        name, found, rest = value.partition(separator)
        if not found or not name.strip():
            raise UploadError(f"{what} must be name{separator}value: {value!r}")
        pairs[name.strip()] = rest.strip()
    return pairs
