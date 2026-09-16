"""Build the run's ``TrialObserver`` from ``observability.tracing`` (ADR-0046).

``exporter: none`` (the default) gives the no-op observer; ``exporter: otlp`` needs the ``otel``
extra and an ``endpoint`` and produces the OTLP observer. The run's tracing identity (the external
``run_id`` a workflow hands in, else the engine's run id, plus the ``run_tag`` namespace) is
returned alongside so the conductor derives the same trace ids the offline bundle uploader will,
and is written to ``run_identity.json`` in the run directory for that uploader to read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.observability import ids
from tolokaforge.observability.model_names import (
    RESERVED_TAG_PREFIXES,
    ModelNameResolverError,
    build_model_name_resolver,
)
from tolokaforge.observability.observer import NullTrialObserver, TrialIdentity, TrialObserver

if TYPE_CHECKING:
    from tolokaforge.core.models import ObservabilityConfig

RUN_IDENTITY_FILE = "run_identity.json"
TRACING_RECEIPT_FILE = "tracing_receipt.json"
_TAG_SHAPE = re.compile(r"^[a-z][a-z0-9_]*:\S+$")


class TracingConfigError(ValueError):
    """``observability.tracing`` cannot be honoured as written."""


@dataclass(frozen=True)
class RunIdentity:
    """The two id-contract components every trial of a run shares."""

    run_id: str
    run_tag: str = ids.DEFAULT_RUN_TAG

    def trial(self, task_id: str, trial_index: int, attempt_id: int) -> TrialIdentity:
        return TrialIdentity(
            run_id=self.run_id,
            task_id=task_id,
            trial_index=trial_index,
            attempt_id=attempt_id,
            run_tag=self.run_tag,
        )


def validate_tag(tag: str) -> str:
    """A caller tag is ``<prefix>:<value>``; the prefixes the exporter derives itself are refused."""
    if not _TAG_SHAPE.match(tag):
        raise TracingConfigError(f"tracing tag {tag!r} must look like <prefix>:<value>")
    if tag.partition(":")[0] in RESERVED_TAG_PREFIXES:
        raise TracingConfigError(f"tracing tag {tag!r}: its prefix is set by the exporter itself")
    return tag


def build_trial_observer(
    observability: ObservabilityConfig | None,
    *,
    engine_run_id: str,
    output_dir: Path | None = None,
) -> tuple[TrialObserver, RunIdentity]:
    """The observer for this run and the identity its trials trace under."""
    from tolokaforge.core.models import TracingConfig

    tracing = getattr(observability, "tracing", None)
    if not isinstance(tracing, TracingConfig):  # absent, or a caller's stub config
        tracing = None
    run_id = (tracing.run_id or engine_run_id) if tracing else engine_run_id
    run_tag = (tracing.run_tag or ids.DEFAULT_RUN_TAG) if tracing else ids.DEFAULT_RUN_TAG
    try:
        ids.check_component("run_id", run_id)
        ids.check_component("run_tag", run_tag)
    except ValueError as exc:
        raise TracingConfigError(str(exc)) from exc
    identity = RunIdentity(run_id=run_id, run_tag=run_tag)
    if tracing is None or tracing.exporter == "none":
        return NullTrialObserver(), identity
    if not tracing.endpoint:
        raise TracingConfigError("observability.tracing.exporter='otlp' requires an endpoint")
    for tag in tracing.tags:
        validate_tag(tag)
    try:
        from tolokaforge.observability.otel import OTelTrialObserver, SpanQueue, make_otlp_exporter
    except ImportError as exc:
        raise TracingConfigError(
            "observability.tracing.exporter='otlp' needs the 'otel' extra: pip install 'tolokaforge[otel]'"
        ) from exc
    try:
        resolver = build_model_name_resolver(
            tracing.model_name_normalizer, tracing.model_name_rules
        )
    except ModelNameResolverError as exc:
        raise TracingConfigError(str(exc)) from exc
    queue = SpanQueue(
        make_otlp_exporter(tracing.endpoint, headers=otlp_headers()),
        max_size=tracing.queue_size,
        batch_size=tracing.export_batch_size,
        interval_s=tracing.export_interval_s,
    )
    observer = OTelTrialObserver(
        queue=queue,
        resolver=resolver,
        label=tracing.label or (Path(output_dir).name if output_dir else engine_run_id),
        session_id=tracing.session_id or run_id,
        tags=tracing.tags,
        metadata=dict(tracing.metadata),
        service_name=tracing.service_name,
        attribute_max_chars=tracing.attribute_max_chars,
        context_messages=tracing.context_messages,
        flush_timeout_s=tracing.flush_timeout_s,
        attachments=build_attachments(tracing),
    )
    if output_dir is not None:
        write_run_identity(Path(output_dir), identity)
    return observer, identity


OTLP_HEADERS_SECRET = "OTEL_EXPORTER_OTLP_HEADERS"
_SECRET_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE|SIGNING|COOKIE|SESSION)", re.I
)
_NOT_SECRET_NAME = re.compile(
    r"(PUBLIC_KEY_ID|_FILE$|_PATH$|_DIR$|_URL$|_BASE$|_NAME$|_HEADER$)", re.I
)


def build_attachments(tracing: Any) -> Any:
    """The post-trial attachment step for ``observability.tracing.attach`` (``None`` for
    ``none``): the receiver's REST base derives from the OTLP endpoint unless given, the headers
    are the OTLP exporter's, the data-safety scan knows the ``SecretManager``'s credential
    values (keys with secret-like names; URL, path and name values are not credentials and a
    bundle may legitimately quote them)."""
    from tolokaforge.observability.attachments import ATTACH_NONE, SecretScan
    from tolokaforge.observability.langfuse_media import (
        LangfuseAttachments,
        api_base_from_endpoint,
    )

    if getattr(tracing, "attach", ATTACH_NONE) == ATTACH_NONE:
        return None
    return LangfuseAttachments(
        api_base=tracing.attach_api_base or api_base_from_endpoint(tracing.endpoint),
        headers=otlp_headers() or {},
        mode=tracing.attach,
        scan=SecretScan(secret_values()),
        timeout_s=tracing.attach_timeout_s,
    )


def secret_values() -> list[str]:
    """The credential values the ``SecretManager`` resolves under secret-like key names."""
    try:
        from tolokaforge.secrets import get_default_or_none
    except ImportError:  # pragma: no cover - the secrets package is part of core
        return []
    manager = get_default_or_none()
    if manager is None:
        return []
    values: list[str] = []
    for key in manager.list_all_keys():
        if not _SECRET_NAME.search(key) or _NOT_SECRET_NAME.search(key):
            continue
        value = manager.get_secret(key)
        if value:
            values.append(value)
    return values


def otlp_headers() -> dict[str, str] | None:
    """The receiver's request headers from the ``SecretManager`` (so the value sits in the
    log-redaction set), parsed from the OTLP ``key=value,key2=value2`` form; ``None`` when unset,
    in which case the SDK's own environment lookup applies."""
    try:
        from tolokaforge.secrets import get_default_or_none
    except ImportError:  # pragma: no cover - the secrets package is part of core
        return None
    manager = get_default_or_none()
    raw = manager.get_secret(OTLP_HEADERS_SECRET) if manager is not None else None
    if not raw:
        return None
    headers: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            headers[key.strip()] = value.strip()
    return headers or None


def write_run_identity(output_dir: Path, identity: RunIdentity) -> Path:
    """``run_identity.json`` next to ``trials/``: the identity the offline uploader must reuse."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / RUN_IDENTITY_FILE
    path.write_text(
        json.dumps(
            {
                "run_id": identity.run_id,
                "run_tag": identity.run_tag,
                "written_by": "tolokaforge",
                "written_at": datetime.now(tz=timezone.utc).isoformat(),
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_tracing_receipt(output_dir: Path, receipt: dict[str, Any]) -> Path:
    path = Path(output_dir) / TRACING_RECEIPT_FILE
    path.write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    return path
