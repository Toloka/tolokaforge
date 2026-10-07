"""Send a pipeline agent's transcripts to the tracing receiver.

The agents a pipeline runs in CI (the resolve loop and the finalize step of a model integration,
the dimension agents of an evaluation's analysis) write their Claude Code output to the runner
and nowhere else: it is never an artifact, because a tool result can carry the run's credentials.
This is the one step that takes those files off the runner, and it is deliberately narrow.

What it does, in order, per file:

1. **read** the output through :mod:`tolokaforge_langfuse.transcripts`, whose allowlist refuses a
   shape it does not know rather than guessing at it, and the prompt the agent was given from
   ``<stem>.prompt.txt`` beside it when its caller kept one: the output does not repeat the
   prompt, and it becomes the trace's input;
2. **gate** it: the tool input/output policy (``drop`` by default, so what a tool returned never
   leaves the runner at all);
3. **project** it to ingestion bodies and then to OTLP spans, the same two steps the trial path
   takes, so a transcript and a trial read alike in the same UI;
4. **scan** the payload against the sentinel, which knows the credential shapes *and* the values
   this very process holds: the shapes read the serialised JSON, the values also the raw strings.
   A hit sends nothing;
5. **export** one batch per transcript, under the wheel's retry policy (its defaults): a batch the
   receiver refused without reading it (the gateway's block page, 429, 503) is posted again after
   a wait, and nothing else is. The upload as a whole waits at most the policy's
   ``flush_grace_s``, so a receiver that keeps refusing costs one grace, not one per transcript.

Nothing here fails the pipeline on its own: the command reports what it refused, what it blocked
and what it could not send and exits 1 for any of them, for a step that carries
``continue-on-error`` to show without failing the job. Credentials are
read from the step's own environment through the ``SecretManager``, never logged, and never
written to the receipt.

The wheel is imported inside :func:`upload`, so the tool's other commands never load the
OpenTelemetry exporter.
"""

from __future__ import annotations

import http.client
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

from tolokaforge.secrets import EnvProvider, SecretManager

OTLP_PATH = "/api/public/otel/v1/traces"
PROJECTS_PATH = "/api/public/projects"
OBSERVATIONS_PATH = "/api/public/v2/observations"
# ``core`` alone has no environment and ``basic`` brings it in the same request, no second round trip
OBSERVATION_FIELDS = "core,basic"
READ_PAGE = 100
# a transcript's trace is a few hundred observations; a walk past this many pages is not paging
MAX_PAGES = 50
# a row the receiver filed under no environment of its own sits in its default
DEFAULT_ENVIRONMENT = "default"
DEFAULT_RUN_TAG = "v1"
# the model-name resolvers the engine's tracing configuration knows: the raw names, or the
# toloka-model-name-normalizer under a deployment's rules
MODEL_NAME_NORMALIZERS = ("none", "toloka")
READ_TIMEOUT_S = 10.0

# ``agent_iter_3.jsonl`` is the third resolve iteration; ``agent_finalize.jsonl`` is the finalize
# step. A file named on the command line under any other name keeps its own stem.
_ITERATION = re.compile(r"^agent_iter_(\d+)$")
# ``analysis_<dimension>.json`` is one dimension agent of an evaluation's analysis, and
# ``analysis_<dimension>.<n>.json`` a later run of the same dimension in the same directory. The
# writer keeps ``[A-Za-z0-9_.-]`` of the dimension and numbers its later runs from 2, so only a
# trailing ``.<n>`` from 2 up is a run number: ``harness_infra`` stays whole and ``v1.5_x`` keeps
# its dot. A dimension that itself ends in such a number cannot be told from a later run by its
# file name, here or anywhere else.
_ANALYSIS = re.compile(r"^analysis_(?P<dimension>[A-Za-z0-9_.-]+?)(?:\.(?P<run>[2-9]|[1-9]\d+))?$")
# The prompt an agent run was given, kept by its caller beside the output under the output's own
# stem: ``analysis_four_bucket.2.json`` -> ``analysis_four_bucket.2.prompt.txt``. Only ``*.json``
# and ``*.jsonl`` are listed, so a prompt file is never read as a transcript nor listed as not
# read, and one without its transcript is not read at all.
PROMPT_SUFFIX = ".prompt.txt"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the key pair and the admission header to whatever host it names, and
    its answer would pass for the receiver's: a 3xx is an answer the reads cannot use."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class UploadError(RuntimeError):
    """The upload cannot start: no receiver, no credentials, keys that open another project, or
    a malformed input."""


@dataclass(frozen=True)
class Receiver:
    """Where the spans go and what admits them. The headers never reach a log or a receipt."""

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    base_url: str | None = None

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, *, secrets: SecretManager | None = None
    ) -> Receiver:
        """The address from ``env``; the key pair and the admission headers through ``secrets``,
        env-only by default: the workflow maps the secrets in per step, and dotenv precedence
        would let a developer's local ``.env`` answer a CI run."""
        env = env if env is not None else os.environ
        secrets = secrets if secrets is not None else SecretManager([EnvProvider()])
        base = (env.get("LANGFUSE_BASE_URL") or "").rstrip("/")
        endpoint = env.get("LANGFUSE_OTLP_ENDPOINT") or (f"{base}{OTLP_PATH}" if base else "")
        if not endpoint:
            raise UploadError("no receiver: set LANGFUSE_BASE_URL or LANGFUSE_OTLP_ENDPOINT")
        public = secrets.get_secret("LANGFUSE_PUBLIC_KEY")
        secret = secrets.get_secret("LANGFUSE_SECRET_KEY")
        if not public or not secret:
            raise UploadError("no credentials: set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        token = b64encode(f"{public}:{secret}".encode()).decode()
        headers = {"Authorization": f"Basic {token}"}
        headers.update(_extra_headers(secrets.get_secret("LANGFUSE_EXTRA_HEADERS")))
        return cls(endpoint=endpoint, headers=headers, base_url=base or None)

    def _get(self, path: str) -> Any:
        """One REST read, or ``None`` when the receiver's REST API cannot be reached: an alias in
        front of it may route the OTLP endpoint and nothing else."""
        if not self.base_url:
            return None
        request = urllib.request.Request(f"{self.base_url}{path}", headers=self.headers)
        try:
            with _OPENER.open(request, timeout=READ_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            ValueError,
            OSError,
        ):
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
        cursors: set[str] = set()
        while True:
            query = (
                f"traceId={quote(trace_id, safe='')}&fields={OBSERVATION_FIELDS}&limit={READ_PAGE}"
            )
            if cursor:
                query += f"&cursor={quote(cursor, safe='')}"
            payload = self._get(f"{OBSERVATIONS_PATH}?{query}")
            rows = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                # no answer, or one that is not the observations API (a proxy's own page)
                return None
            page = list(rows)
            found.update(str(row.get("environment") or DEFAULT_ENVIRONMENT) for row in page)
            meta = payload.get("meta")
            cursor = str(meta.get("cursor") or "") if isinstance(meta, Mapping) else ""
            # a short page ends the walk whatever the cursor says: this read must never spin
            if not cursor or len(page) < READ_PAGE:
                return found
            if cursor in cursors or len(cursors) >= MAX_PAGES:
                # a cursor handed back twice, or a walk without end, is not paging
                return None
            cursors.add(cursor)


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
        # the value carries the gateway's admission key: name its size, never its text
        raise UploadError(
            f"LANGFUSE_EXTRA_HEADERS ({len(raw)} characters) yields no header; it is a "
            "comma-separated list of name=value pairs"
        )
    return headers


def is_agent_output(path: Path) -> bool:
    """Whether a file is one of the agents' own outputs: an integration's resolve iterations and
    finalize step (the names cost_summary reads too), or an evaluation analysis's dimension agents.

    A stage directory also holds the compose step's ``decision.json`` and the reprobe findings,
    which are not transcripts and must not count as refused ones.
    """
    stem = path.stem
    return stem == "agent_finalize" or bool(_ITERATION.match(stem) or _ANALYSIS.match(stem))


def transcript_id_for(path: Path) -> str:
    """The id a transcript file takes in the trace id's task position."""
    stem = path.stem
    match = _ITERATION.match(stem)
    if match:
        return f"resolve/{int(match.group(1))}"
    if stem == "agent_finalize":
        return "finalize"
    analysis = _ANALYSIS.match(stem)
    if analysis:
        dimension, run = analysis.group("dimension", "run")
        return f"analysis/{dimension}/{run}" if run else f"analysis/{dimension}"
    return stem


def prompt_path(path: Path) -> Path:
    """Where the prompt of the agent run in ``path`` is kept: ``<stem>.prompt.txt`` beside it."""
    return path.with_name(path.stem + PROMPT_SUFFIX)


def read_prompt(path: Path) -> str | None:
    """The prompt of the agent run in ``path``, or ``None`` when its caller kept none.

    Anything at that name that is not a regular file of UTF-8 text (a directory, a dangling link,
    a named pipe, a device, bytes that are not UTF-8) raises (``OSError``,
    ``UnicodeDecodeError``): its transcript is refused rather than sent as though the agent had
    been given no prompt. A file that is not regular is refused before it is opened, because a
    pipe blocks the open and a device never ends.
    """
    prompt = prompt_path(path)
    if not prompt.exists() and not prompt.is_symlink():
        return None
    if not prompt.is_file():
        raise OSError("not a regular file")
    return prompt.read_text(encoding="utf-8")


@dataclass
class UploadReport:
    """What happened, in a shape a job summary and a receipt can both read."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, str]] = field(default_factory=list)
    blocked: list[dict[str, str]] = field(default_factory=list)
    mismatched: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    # sent, but the environment guard could not ask the receiver first (not a failure)
    unchecked: list[dict[str, str]] = field(default_factory=list)
    # files in the directory that are not an agent's output (not read, not a failure)
    ignored: list[str] = field(default_factory=list)
    # what the retry policy did (tolokaforge_langfuse.retry.RetryStats.counts)
    retries: dict[str, int] = field(default_factory=dict)
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
            "unchecked": self.unchecked,
            "ignored": self.ignored,
            "retries": self.retries,
            "ok": self.ok,
        }

    def as_markdown(self) -> str:
        prompts = sum(1 for entry in self.sent if entry.get("prompt"))
        lines = [
            "### Agent transcripts",
            "",
            f"- sent: **{len(self.sent)}** transcript(s), {self.spans} span(s)"
            + (f", {prompts} with the agent's prompt as input" if prompts else "")
            + (" (dry run, nothing left the runner)" if self.dry_run else ""),
        ]
        for label, entries in (
            ("refused by the reader", self.refused),
            ("blocked by the safety gate", self.blocked),
            ("already in another environment", self.mismatched),
            ("not sent", self.failed),
            ("sent without the environment check", self.unchecked),
        ):
            if entries:
                lines.append(f"- {label}: **{len(entries)}**")
                lines.extend(f"  - `{e['file']}`: {e['reason']}" for e in entries)
        if self.ignored:
            lines.append(f"- not an agent output file, not read: **{len(self.ignored)}**")
            lines.extend(f"  - `{name}`" for name in self.ignored)
        if self.retries.get("retried_requests") or self.retries.get("retries_exhausted"):
            lines.append(
                f"- posted again after a refusal: **{self.retries['retried_requests']}** "
                f"transcript(s), {self.retries['retries_recovered']} recovered, "
                f"{self.retries['retries_exhausted']} given up, "
                f"{self.retries['retry_wait_s']} s waited"
            )
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
    model: str | None = None,
    trace_name: str | None = None,
    user: str | None = None,
    user_model: str | None = None,
    model_name_normalizer: str = "none",
    model_rules: str | None = None,
    producer_version: str = "automation",
    receiver: Receiver | None = None,
    dry_run: bool = False,
) -> UploadReport:
    """Read, gate, project and send every agent transcript under ``directory``.

    ``model`` is the model that served the runs when the CLI was pointed at an alias: the CLI
    reports the alias, and a gateway routes it to whatever it is configured to serve.
    ``trace_name`` is the traces' name template (``{label}``, ``{transcript}``, ``{step}``; default
    ``{label}/{transcript}``). The traces' user is ``user`` as given, or the identity of
    ``user_model``, the model the agents worked on. Model names are read through the resolver
    ``model_name_normalizer`` selects (``none``, the raw names, or ``toloka`` with the deployment's
    ``model_rules``), the trial traces' rule, so a model has one name across both.
    """
    from tolokaforge_langfuse.model_names import (
        ModelNameResolverError,
        build_model_name_resolver,
    )
    from tolokaforge_langfuse.retry import Retrier, RetryPolicy

    from tolokaforge.observability import ids as engine_ids
    from tolokaforge_langfuse import otlp_spans, otlp_transport
    from tolokaforge_langfuse import transcripts as tr

    if model_name_normalizer not in MODEL_NAME_NORMALIZERS:
        # a misspelt choice would read every name raw and silently differ from the trial traces
        raise UploadError(
            f"--model-name-normalizer {model_name_normalizer!r} is not one of "
            f"{', '.join(MODEL_NAME_NORMALIZERS)}"
        )
    try:
        resolver = build_model_name_resolver(model_name_normalizer, model_rules)
    except ModelNameResolverError as exc:
        raise UploadError(f"the model-name resolver cannot be built: {exc}") from exc

    # surrounding whitespace goes, as from a --tag or --metadata value: a trailing newline read
    # from a file would otherwise end up inside the model tag
    served_model = (model or "").strip() or None

    files = tr.transcript_files(Path(directory))
    ignored: list[str] = []
    if Path(directory).is_dir():
        # a named file is read as given; a directory's other files are the stage's own records
        ignored = [path.name for path in files if not is_agent_output(path)]
        files = [path for path in files if is_agent_output(path)]
    report = UploadReport(dry_run=dry_run, ignored=ignored)
    if not files:
        return report

    if receiver is None and not dry_run:
        receiver = Receiver.from_environment()
    verified = _project_verified(receiver, project)
    gate = _sentinel(receiver)
    contract = tr.id_contract(engine_ids)
    retrier = Retrier(RetryPolicy())
    # the whole upload waits for refusals at most one grace, the bound a live run's end has
    retrier.set_deadline_after(retrier.policy.flush_grace_s)
    exporter = (
        otlp_transport.make_otlp_exporter(
            receiver.endpoint, receiver.headers, retry=False, retrier=retrier
        )
        if receiver is not None and not dry_run
        else None
    )

    for path in files:
        name = path.name
        try:
            prompt = read_prompt(path)
        except (OSError, UnicodeDecodeError) as exc:
            report.refused.append(
                {"file": name, "reason": f"{prompt_path(path).name} cannot be read: {exc}"}
            )
            continue
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
                model=served_model,
                input=prompt,
                name=trace_name,
                user=user,
                user_model=user_model,
                resolver=resolver,
            )
            built = tr.build_events(gated, options, ids=contract)
        except tr.TranscriptError as exc:
            report.refused.append({"file": name, "reason": str(exc)})
            continue

        findings = _scan(gate, built.events, what=name)
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
        if exporter is not None:
            try:
                outcome = exporter.export(spans)
            except Exception as exc:  # the receiver is not this pipeline's business to fail on
                report.failed.append({"file": name, "reason": f"export raised: {exc}"})
                continue
            if getattr(outcome, "name", str(outcome)) != "SUCCESS":
                report.failed.append({"file": name, "reason": f"export returned {outcome}"})
                continue
        entry: dict[str, Any] = {
            "file": name,
            "transcript_id": gated.transcript_id,
            "trace_id": built.trace_id,
            "spans": len(spans),
        }
        if prompt is not None:
            entry["prompt"] = prompt_path(path).name
        report.sent.append(entry)
        if elsewhere is None:
            report.unchecked.append(
                {
                    "file": name,
                    "reason": "the receiver's REST API could not be asked which environment "
                    "already holds this trace",
                }
            )
    report.retries = retrier.stats.counts()
    return report


def _sentinel(receiver: Receiver | None) -> Any:
    """The outbound sentinel: the secrets of the process, and the receiver's own header values,
    because the admission key arrives as LANGFUSE_EXTRA_HEADERS, a name the gate does not read as
    a secret's."""
    from tolokaforge_langfuse import safety

    held = {
        f"header {name}": value for name, value in (receiver.headers if receiver else {}).items()
    }
    return safety.SafetyGate.from_environment(extra=held)


def _scan(gate: Any, events: Sequence[Mapping[str, Any]], *, what: str) -> list[Any]:
    """The sentinel over the events: the shapes over their JSON, the values the process holds also
    over every raw string and its JSON-escaped forms. The wheel's one rule, the one every live
    span and the trial-end pass use (``SafetyGate.scan_structured``)."""
    return list(gate.scan_structured(events, what=what))


def _project_verified(receiver: Receiver | None, project: str | None) -> bool:
    """Whether the keys are known to open ``project``; ``False`` when there is nothing to check or
    the receiver cannot be asked. Keys that open another project refuse the upload (the live
    observer's rule): every trace would carry a ``project:`` tag its own receiver contradicts."""
    if not project or receiver is None:
        # an empty --project or LANGFUSE_PROJECT names nothing: the trace says project:none
        return False
    opened = receiver.project_name()
    if opened is not None and opened != project:
        raise UploadError(
            f"the keys open project {opened!r}, not {project!r}: fix the keys, never the "
            "expectation"
        )
    return opened == project


def _held_elsewhere(
    receiver: Receiver | None, trace_id: str, environment: str | None
) -> set[str] | None:
    """The environments this trace already lives in, other than the one we are about to write.

    Empty when there is nothing there, or when there is no receiver at all (a dry run); ``None``
    when the question cannot be asked, which the report records rather than hides. Not a
    substitute for the deployment pinning one environment per set of keys: a guard against the
    one failure mode a v4 receiver makes invisible.
    """
    if receiver is None:
        return set()
    found = receiver.environments_of(trace_id)
    if found is None:
        return None
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
