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
from urllib.parse import quote

OTLP_PATH = "/api/public/otel/v1/traces"
PROJECTS_PATH = "/api/public/projects"
OBSERVATIONS_PATH = "/api/public/v2/observations"
# ``core`` alone has no environment and ``basic`` brings it in the same request, no second round trip
OBSERVATION_FIELDS = "core,basic"
READ_PAGE = 100
# a row the receiver filed under no environment of its own sits in its default
DEFAULT_ENVIRONMENT = "default"
DEFAULT_RUN_TAG = "v1"
READ_TIMEOUT_S = 10.0

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

    def _get(self, path: str) -> Any:
        """One REST read, or ``None`` when the receiver's REST API cannot be reached: an alias in
        front of it may route the OTLP endpoint and nothing else."""
        if not self.base_url:
            return None
        request = urllib.request.Request(f"{self.base_url}{path}", headers=self.headers)
        try:
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None

    def project_name(self) -> str | None:
        """The receiver's own name for the project these keys open, or ``None`` when it cannot be
        asked."""
        payload = self._get(PROJECTS_PATH)
        projects = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(projects, Sequence) and projects:
            first = projects[0]
            if isinstance(first, Mapping):
                return str(first.get("name") or "") or None
        return None

    def environments_of(self, trace_id: str) -> set[str] | None:
        """Every environment the receiver already holds rows of this trace in.

        A v4 receiver merges observations **by id alone**: re-sending a trace under a second
        environment is a silent no-op that reports success and leaves every row where it first
        landed. So the environment a trace already lives in is the one fact worth a read before a
        write. ``None`` means the question could not be asked, which is not the same as "nowhere".
        """
        found: set[str] = set()
        cursor = ""
        while True:
            query = (
                f"traceId={quote(trace_id, safe='')}&fields={OBSERVATION_FIELDS}&limit={READ_PAGE}"
            )
            if cursor:
                query += f"&cursor={quote(cursor, safe='')}"
            payload = self._get(f"{OBSERVATIONS_PATH}?{query}")
            if payload is None:
                return None
            rows = (payload.get("data") if isinstance(payload, Mapping) else None) or []
            page = [row for row in rows if isinstance(row, Mapping)]
            found.update(str(row.get("environment") or DEFAULT_ENVIRONMENT) for row in page)
            meta = (payload.get("meta") or {}) if isinstance(payload, Mapping) else {}
            cursor = str(meta.get("cursor") or "")
            # a short page ends the walk whatever the cursor says: this read must never spin
            if not cursor or len(page) < READ_PAGE:
                return found


def _extra_headers(raw: str | None) -> dict[str, str]:
    """Headers a network admission layer in front of the receiver needs: ``k=v,k2=v2``.

    The spelling is the live observer's (``tolokaforge_langfuse.plugin``), because both read the
    same variable and a CI job sets it once for whatever writes from that runner. A malformed
    item is skipped there, and skipped here, so neither is stricter than the other about what it
    accepts. What IS an error is a value that yields no header at all: the variable exists only
    because something in front of the receiver refuses requests without it, and the refusal that
    follows a typo is an unexplained 403 in a job log.
    """
    if not raw or not raw.strip():
        return {}
    headers: dict[str, str] = {}
    for item in raw.split(","):
        name, separator, value = item.partition("=")
        if separator and name.strip():
            headers[name.strip()] = value.strip()
    if not headers:
        raise UploadError(
            f"LANGFUSE_EXTRA_HEADERS={raw!r} yields no header; it is a comma-separated list of "
            "name=value pairs"
        )
    return headers


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
    mismatched: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not (self.refused or self.blocked or self.mismatched or self.failed)

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
            "mismatched": self.mismatched,
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
            ("already in another environment", self.mismatched),
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

        elsewhere = _held_elsewhere(receiver, built.trace_id, environment)
        if elsewhere:
            report.mismatched.append(
                {
                    "file": name,
                    "reason": f"the receiver already holds this trace in "
                    f"{', '.join(sorted(elsewhere))}; sending it as "
                    f"{environment or DEFAULT_ENVIRONMENT} would be a silent no-op",
                }
            )
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


def _held_elsewhere(receiver: Receiver | None, trace_id: str, environment: str | None) -> set[str]:
    """The environments this trace already lives in, other than the one we are about to write.

    Empty when there is nothing there, when the question cannot be asked, or when there is no
    receiver at all (a dry run). Not a substitute for the deployment pinning one environment per
    set of keys: a guard against the one failure mode a v4 receiver makes invisible.
    """
    if receiver is None:
        return set()
    found = receiver.environments_of(trace_id)
    if found is None:
        return set()
    return found - {environment or DEFAULT_ENVIRONMENT}


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
