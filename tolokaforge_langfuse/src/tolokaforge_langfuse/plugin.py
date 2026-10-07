"""The Langfuse trial observer as a plugin of the engine's ``TrialObserver`` seam (ADR-0047,
packaging amendment).

The engine finds this module through the ``tolokaforge.trial_observers`` entry-point group
(``langfuse = tolokaforge_langfuse.plugin:build``), resolves the run's tracing identity itself and
hands :func:`build` the run config's ``observability.tracing`` block. This module decides whether
the receiver is wanted at all (``exporter: otlp``, or the one switch ``LANGFUSE_TRACING_ENABLED``),
where it is, how to authenticate, which deployment profile applies, and returns the observer, or
``None`` when nothing asks for it. It raises the engine's ``TracingConfigError`` for a configuration
it cannot honour, at run start, before any service starts; nothing raises into a trial later.

A launcher that owns the receiver (the Langfuse connector's ``with-environment``) injects the
endpoint, the headers, extra tags (``TOLOKAFORGE_TRACING_TAGS``) and the project the credentials
must open (``TOLOKAFORGE_TRACING_EXPECT_PROJECT``); ``expect_project`` is checked against the
receiver before the first export and a mismatch refuses to trace (ADR-0047, destinations
amendment).

One switch (ADR-0047, Langfuse switch amendment): ``LANGFUSE_TRACING_ENABLED=true`` turns the
exporter on without a config block. The receiver then comes from the plain Langfuse variables:
``LANGFUSE_BASE_URL`` (the traces endpoint is ``<base>/api/public/otel/v1/traces``, the REST base
for attachments and gradings is ``<base>``), ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``
(the Basic header, read through the ``SecretManager``), optional
``LANGFUSE_EXTRA_HEADERS`` (``k=v,k2=v2``, a gateway's own header) and optional
``LANGFUSE_PROJECT`` (the project the keys must open; also the ``project:`` tag).

The deployment's configuration (ADR-0047, parity and configuration amendments) is the block
``observability.tracing.options.langfuse``, usually under ``run_defaults`` of the enclosing
``project.yaml``: the profile (inline, or a TOML / YAML path; ``TOLOKAFORGE_TRACING_PROFILE``
without one), the one ``project`` the credentials must open and its native ``environments`` with
what each accepts. :func:`tolokaforge_langfuse.preflight.resolve_plan` turns the block and the
launcher's variables into the run's tags, metadata and environment without any network or engine
call, the same code the offline connector and the CI pre-check run; this module adds the
receiver: endpoint, credentials, the project check, the receiver family and the observer.
Relative paths in the block anchor to the nearest ``project.yaml`` above the working directory.

Live previews (ADR-0048, previews amendment): on a v4 receiver a trial is written once, when it
is persisted, and nothing goes out while it runs. ``LANGFUSE_TRACING_PREVIEWS=true`` also sends
the live rows as declared previews; they stay beside the final rows, and the receiver's own views
count every row. Off by default; a v3 receiver always gets the live rows, which are its record.

The pairing with the engine is checked once per run (:func:`check_engine_api`): the engine's
``PLUGIN_API_VERSION`` must equal this package's ``__api_version__``; a mismatch is a
configuration error naming both versions, so a plugin released ahead of, or behind, the engine
pin fails at run start with the fix in the message.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.observability.factory import RunIdentity, TracingConfigError, engine_version
from tolokaforge.observability.observer import TrialObserver
from tolokaforge_langfuse import __api_version__, __version__
from tolokaforge_langfuse.config import LangfuseConfig

# the receiver families and the projection mode have one home each: the module that owns the
# behaviour behind them (the capability probe, the bundle projection). Neither pulls the
# OpenTelemetry SDK in, which is why they can be imported here and the observer cannot.
from tolokaforge_langfuse.media import SERVER_V3, SERVER_V4
from tolokaforge_langfuse.preflight import (
    PreflightError,
    TracingPlan,
    anchor_directory,
    producer_version,
    resolve_plan,
)
from tolokaforge_langfuse.projection import PROJECTION_FULL
from tolokaforge_langfuse.retry import Retrier, RetryBreaker, RetryStats
from tolokaforge_langfuse.safety import SafetyGate, looks_secret

if TYPE_CHECKING:
    from tolokaforge.core.models import TracingConfig

# the standard OTel receiver variables (the SDK's own names) and the engine's launcher variables
OTLP_TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACING_SESSION_ID_ENV = "TOLOKAFORGE_TRACING_SESSION_ID"
TRACING_LABEL_ENV = "TOLOKAFORGE_TRACING_LABEL"
# the one switch and the plain Langfuse receiver variables
LANGFUSE_ENABLED_ENV = "LANGFUSE_TRACING_ENABLED"
LANGFUSE_PREVIEWS_ENV = "LANGFUSE_TRACING_PREVIEWS"
LANGFUSE_BASE_URL_ENV = "LANGFUSE_BASE_URL"
LANGFUSE_PUBLIC_KEY_SECRET = "LANGFUSE_PUBLIC_KEY"
LANGFUSE_SECRET_KEY_SECRET = "LANGFUSE_SECRET_KEY"
LANGFUSE_EXTRA_HEADERS_SECRET = "LANGFUSE_EXTRA_HEADERS"
LANGFUSE_OTEL_PATH = "/api/public/otel/v1/traces"
_TRUE = frozenset({"1", "true", "yes", "on"})
PROJECT_VERIFIED = "verified"
PROJECT_UNVERIFIED = "unverified"
PROJECT_UNCHECKED = "none"

_log = logging.getLogger(__name__)


def check_engine_api() -> None:
    """The engine must speak this package's plugin contract; the check names both sides."""
    from tolokaforge.observability import factory as engine

    offered = getattr(engine, "PLUGIN_API_VERSION", None)
    if offered != __api_version__:
        raise TracingConfigError(
            f"tolokaforge-langfuse {__version__} speaks trial-observer API v{__api_version__}, "
            f"this engine (tolokaforge {engine_version()}) offers {offered!r}: upgrade the one "
            "that is behind"
        )


def build(
    tracing: TracingConfig | None,
    identity: RunIdentity,
    *,
    engine_run_id: str,
    output_dir: Path | None = None,
) -> TrialObserver | None:
    """The ``tolokaforge.trial_observers`` entry point: the Langfuse observer of this run, or
    ``None`` when nothing asks for it (``exporter: none`` without ``LANGFUSE_TRACING_ENABLED``)."""
    if langfuse_enabled():
        from tolokaforge.core.models import TracingConfig as _TracingConfig

        # the switch: a run without a tracing block, or with exporter none, traces anyway
        tracing = (tracing or _TracingConfig()).model_copy(update={"exporter": "otlp"})
    if tracing is None or tracing.exporter != "otlp":
        return None
    check_engine_api()  # only a run that traces needs the contract to hold
    plan = plan_run(tracing)
    settings = plan.settings
    run_id = identity.run_id
    endpoint = resolve_endpoint(tracing.endpoint)
    try:
        from tolokaforge_langfuse.otel import (
            OTelTrialObserver,
            ProjectionSettings,
            SpanQueue,
        )
        from tolokaforge_langfuse.otlp_transport import (
            INGESTION_VERSION,
            SingleAttemptUnavailable,
            make_otlp_exporter,
        )
    except ImportError as exc:
        raise TracingConfigError(
            "tolokaforge-langfuse needs the OpenTelemetry SDK it depends on: reinstall the package"
        ) from exc
    headers = receiver_headers()
    project_verified = PROJECT_UNCHECKED
    if plan.expect_project:
        project_verified = check_expected_project(settings, endpoint, headers, plan.expect_project)
    server_api = resolve_server_family(settings, endpoint, headers)
    if server_api == SERVER_V4 and settings.projection != PROJECTION_FULL:
        raise TracingConfigError(
            f"observability.tracing.options.langfuse.projection={settings.projection!r} cannot "
            "be used with the v4 write-once layout: a trace's root "
            "observation comes from the persisted bundle and only projection='full' writes one"
        )
    release = engine_release()
    producer = producer_identity()
    version = producer_version(producer, plan.resolver.rules_version, plan.profile)
    gate = live_gate(headers)
    # one count of what the retry policy did, and one breaker that stops its waiting, over the
    # span export and the trial-end calls
    retry_stats = RetryStats()
    retry_breaker = RetryBreaker(settings.retry.breaker_after)
    attachments = build_attachments(
        settings,
        endpoint=endpoint,
        headers=headers,
        environment=plan.environment,
        # in the v4 layout the manifest is part of the root observation, and a legacy
        # trace-create update would be refused anyway
        send_manifest_event=server_api == SERVER_V3,
        gate=gate,
        retry_stats=retry_stats,
        retry_breaker=retry_breaker,
    )
    # the v4 producer posts a batch once, and again only after a refusal that proves the
    # receiver did not read it (ADR-0048); a v3 receiver keeps the SDK's retrying exporter
    span_retrier = (
        Retrier(settings.retry, stats=retry_stats, breaker=retry_breaker)
        if server_api == SERVER_V4
        else None
    )
    try:
        exporter = make_otlp_exporter(
            endpoint,
            headers=headers,
            # the direct ingestion path is a v4 route; the v3 family is written exactly as before
            ingestion_version=INGESTION_VERSION if server_api == SERVER_V4 else None,
            retry=server_api != SERVER_V4,
            retrier=span_retrier,
        )
    except SingleAttemptUnavailable as exc:
        raise TracingConfigError(
            f"the v4 write-once layout requires a single attempt: {exc}"
        ) from exc
    queue = SpanQueue(
        exporter,
        max_size=tracing.queue_size,
        batch_size=tracing.export_batch_size,
        interval_s=tracing.export_interval_s,
        retrier=span_retrier,
        flush_grace_s=settings.retry.flush_grace_s,
    )
    return OTelTrialObserver(
        queue=queue,
        resolver=plan.resolver,
        label=tracing.label
        or _env(TRACING_LABEL_ENV)
        or (Path(output_dir).name if output_dir else engine_run_id),
        session_id=tracing.session_id or _env(TRACING_SESSION_ID_ENV) or run_id,
        tags=plan.tags,
        metadata=plan.metadata,
        service_name=tracing.service_name,
        attribute_max_chars=tracing.attribute_max_chars,
        context_messages=tracing.context_messages,
        flush_timeout_s=tracing.flush_timeout_s,
        attachments=attachments,
        gradings=settings.gradings,
        expect_project=plan.expect_project,
        project_verified=project_verified,
        profile_version=plan.profile.version if plan.profile.present else None,
        projection=ProjectionSettings(
            mode=settings.projection,
            environment=plan.environment,
            release=release,
            version=version,
            producer=producer,
            derived_groups=plan.profile.derived_groups,
            trace_name=plan.profile.trace_name,
            trace_user=plan.profile.trace_user,
        ),
        server_api=server_api,
        previews=langfuse_previews(),
        gate=gate,
        ambient=(run_id, identity.run_tag),
        retry_stats=retry_stats,
    )


def plan_run(tracing: TracingConfig) -> TracingPlan:
    """The run's plan from the engine's merged ``observability.tracing`` and the process
    environment (:func:`tolokaforge_langfuse.preflight.resolve_plan`); relative paths anchor to
    the nearest ``project.yaml`` above the working directory. A plan that cannot be honoured is
    a configuration error at run start."""
    from tolokaforge_langfuse.projection import schema_keys

    try:
        plan = resolve_plan(
            tracing, os.environ, anchor_directory(), reserved_metadata=schema_keys()
        )
    except PreflightError as exc:
        raise TracingConfigError(str(exc)) from exc
    for warning in plan.warnings:
        _log.warning("%s", warning)
    return plan


def engine_release() -> str:
    """The native ``release`` field: this engine's own version, ``tolokaforge-<version>``."""
    return f"tolokaforge-{engine_version()}"


def producer_identity() -> str:
    """The ``uploader_version`` metadata value and the head of the native ``version`` field: this
    package, the code that projects the bundle (the engine's own version is ``release``)."""
    return f"tolokaforge-langfuse-{__version__}"


def resolve_endpoint(configured: str | None) -> str:
    """The receiver's traces URL: the config's ``endpoint``, else the standard OTel variables
    (``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` as is, ``OTEL_EXPORTER_OTLP_ENDPOINT`` + ``/v1/traces``).
    """
    if configured:
        return configured
    traces = os.environ.get(OTLP_TRACES_ENDPOINT_ENV, "").strip()
    if traces:
        return traces
    base = os.environ.get(OTLP_ENDPOINT_ENV, "").strip()
    if base:
        return f"{base.rstrip('/')}/v1/traces"
    langfuse = _env(LANGFUSE_BASE_URL_ENV)
    if langfuse:
        return f"{langfuse.rstrip('/')}{LANGFUSE_OTEL_PATH}"
    raise TracingConfigError(
        "observability.tracing.exporter='otlp' requires an endpoint: set "
        f"observability.tracing.endpoint, {OTLP_TRACES_ENDPOINT_ENV} / {OTLP_ENDPOINT_ENV}, or "
        f"{LANGFUSE_BASE_URL_ENV}"
    )


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def langfuse_enabled() -> bool:
    """``LANGFUSE_TRACING_ENABLED`` is truthy (``1`` / ``true`` / ``yes`` / ``on``)."""
    return (os.environ.get(LANGFUSE_ENABLED_ENV, "").strip().lower()) in _TRUE


def langfuse_previews() -> bool:
    """``LANGFUSE_TRACING_PREVIEWS`` is truthy: a v4 receiver also gets the live rows as declared
    previews while a trial runs (ADR-0048, previews amendment). Off by default."""
    return (os.environ.get(LANGFUSE_PREVIEWS_ENV, "").strip().lower()) in _TRUE


def _secret(name: str) -> str | None:
    """Read a credential through the shared provider chain and log-redaction boundary."""
    from tolokaforge.secrets import get_default

    return get_default().get_secret(name)


def _parse_headers(raw: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in (raw or "").split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def langfuse_headers() -> dict[str, str] | None:
    """The Basic header from ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` plus
    ``LANGFUSE_EXTRA_HEADERS``; ``None`` when the key pair is absent. Half a pair is a
    configuration error: it would otherwise trace unauthenticated and fail late."""
    public = _secret(LANGFUSE_PUBLIC_KEY_SECRET)
    secret = _secret(LANGFUSE_SECRET_KEY_SECRET)
    if not public and not secret:
        return None
    if not (public and secret):
        raise TracingConfigError(
            f"{LANGFUSE_PUBLIC_KEY_SECRET} and {LANGFUSE_SECRET_KEY_SECRET} must be set together"
        )
    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        **_parse_headers(_secret(LANGFUSE_EXTRA_HEADERS_SECRET)),
    }


def receiver_headers() -> dict[str, str] | None:
    """The receiver's request headers: the standard ``OTEL_EXPORTER_OTLP_HEADERS`` when set (a
    launcher owns the receiver), else the Langfuse key pair; the extra headers join either."""
    headers = otlp_headers()
    if headers is not None:
        extra = _parse_headers(_secret(LANGFUSE_EXTRA_HEADERS_SECRET))
        return {**extra, **headers}
    if langfuse_enabled():
        headers = langfuse_headers()
        if headers is None:
            raise TracingConfigError(
                f"{LANGFUSE_ENABLED_ENV} is set but no receiver credentials were found: set "
                f"{LANGFUSE_PUBLIC_KEY_SECRET} / {LANGFUSE_SECRET_KEY_SECRET} or {OTLP_HEADERS_SECRET}"
            )
        return headers
    return langfuse_headers()


def resolve_server_family(
    settings: LangfuseConfig, endpoint: str, headers: dict[str, str] | None
) -> str:
    """Which receiver family this run writes for, resolved once, at run start.

    ``options.langfuse.server_api`` decides when it names a family; ``auto`` asks the receiver
    by capability (``GET /api/public/v2/observations``), never by the version it reports, because
    a v4 server reports ``4.x`` in its legacy and dual write modes too. A receiver that cannot be
    asked leaves the run on the v3 family, which is what every deployment runs today; the choice
    lands in the receipt either way.
    """
    from tolokaforge_langfuse.media import (
        SERVER_AUTO,
        api_base_from_endpoint,
        detect_server_family,
    )

    if settings.server_api != SERVER_AUTO:
        return settings.server_api
    if not headers:
        return SERVER_V3
    api_base = settings.attach_api_base or api_base_from_endpoint(endpoint)
    try:
        family = detect_server_family(api_base, headers, timeout_s=settings.attach_timeout_s)
    except Exception as exc:  # noqa: BLE001 - an unreachable receiver is not a config error
        _log.warning(
            "the receiver's family could not be probed (%s); writing for the v3 family",
            type(exc).__name__,
        )
        return SERVER_V3
    return family


def check_expected_project(
    settings: LangfuseConfig, endpoint: str, headers: dict[str, str] | None, expected: str
) -> str:
    """The fail-closed project check of the live path: the credentials in the headers must open
    ``expected`` on the receiver (Langfuse ``GET /api/public/projects``). A mismatch, a 401 (the
    credentials open no project at all) and missing headers refuse to trace (TracingConfigError,
    before any service starts); a receiver that does not answer, or answers 403 (the external
    ingest alias) or without a project list, logs a warning and returns ``unverified``; a match
    ``verified``."""
    from tolokaforge_langfuse.media import (
        LangfuseApiError,
        api_base_from_endpoint,
        list_projects,
    )

    if not headers:
        raise TracingConfigError(
            f"observability.tracing.options.langfuse.expect_project={expected!r} "
            "needs the receiver credentials in "
            f"{OTLP_HEADERS_SECRET} to check the project; none were found"
        )
    api_base = settings.attach_api_base or api_base_from_endpoint(endpoint)
    try:
        names = list_projects(api_base, headers, timeout_s=settings.attach_timeout_s)
    except LangfuseApiError as exc:
        if exc.status == 401:
            raise TracingConfigError(
                f"observability.tracing.options.langfuse.expect_project={expected!r} "
                "but the receiver rejected the "
                "credentials (HTTP 401): they open no project; tracing refused"
            ) from exc
        _log.warning(
            "expect_project=%s not verified: %s did not confirm a project (%s)",
            expected,
            api_base,
            exc,
        )
        return PROJECT_UNVERIFIED
    except Exception as exc:  # noqa: BLE001 - the receiver is not reachable: unverified, not fatal
        _log.warning(
            "expect_project=%s not verified: the projects endpoint of %s did not answer (%s)",
            expected,
            api_base,
            type(exc).__name__,
        )
        return PROJECT_UNVERIFIED
    if expected in names:
        return PROJECT_VERIFIED
    raise TracingConfigError(
        f"observability.tracing.options.langfuse.expect_project={expected!r} "
        "but the receiver credentials open "
        f"{names!r}; tracing refused (fix the credentials, never the expectation)"
    )


OTLP_HEADERS_SECRET = "OTEL_EXPORTER_OTLP_HEADERS"


def build_attachments(
    settings: LangfuseConfig,
    *,
    endpoint: str,
    headers: dict[str, str] | None = None,
    environment: str | None = None,
    send_manifest_event: bool = True,
    gate: SafetyGate | None = None,
    retry_stats: RetryStats | None = None,
    retry_breaker: RetryBreaker | None = None,
) -> Any:
    """The post-trial step (the attachments and the ingestion route of the trial-end pass) for
    ``observability.tracing.options.langfuse.attach`` / ``projection``; ``None`` only when nothing
    runs at trial end (``attach: none`` and ``projection: none``, or ``projection: gradings`` with
    ``gradings: false``). The receiver's REST base derives from the OTLP endpoint unless given,
    the headers are the OTLP exporter's, the data-safety scan is the run's ``gate`` (the observer's
    own, so one known set; without one, the ``SecretManager``'s credential values: keys with
    secret-like names, where URL, path and name values are not credentials and a bundle may
    legitimately quote them), and ``environment`` rides on the manifest update too.
    ``send_manifest_event`` is false in the v4 layout, where the manifest is part of the root
    observation instead of a ``trace-create`` update. Its writes follow ``settings.retry`` within
    each trial's budget, counted in ``retry_stats``; while ``retry_breaker`` (the span export's)
    is open they wait no more, and they never count towards it."""
    from tolokaforge_langfuse.attachments import ATTACH_NONE, SecretScan
    from tolokaforge_langfuse.media import (
        LangfuseAttachments,
        api_base_from_endpoint,
    )

    projection = settings.projection
    sends_at_trial_end = projection == "full" or (projection == "gradings" and settings.gradings)
    if settings.attach == ATTACH_NONE and not sends_at_trial_end:
        return None
    return LangfuseAttachments(
        api_base=settings.attach_api_base or api_base_from_endpoint(endpoint),
        headers=(headers if headers is not None else otlp_headers()) or {},
        mode=settings.attach,
        scan=SecretScan(gate=gate) if gate is not None else SecretScan(secret_values()),
        timeout_s=settings.attach_timeout_s,
        budget_s=settings.attach_budget_s,
        environment=environment,
        send_manifest_event=send_manifest_event,
        # the trial ends obey the span export's breaker without counting towards it: a run's
        # parallel trials would otherwise open it within one schedule of a long refusal
        retrier=Retrier(
            settings.retry, stats=retry_stats, breaker=retry_breaker, trips_breaker=False
        ),
    )


def live_gate(headers: Mapping[str, str] | None = None) -> SafetyGate:
    """The data-safety gate of a run: every live span, the trial-end passes and the file
    attachments scan with it, one known set. It knows the credentials this process holds (its
    environment and the ``SecretManager``'s keys) and the receiver's header values, by name, and
    re-reads them when the default ``SecretManager`` is replaced (a secret registered at run
    time, ``register_runtime_secret``)."""
    gate = _read_gate(headers)
    gate.reload = _reloader(headers)
    return gate


def _read_gate(headers: Mapping[str, str] | None) -> SafetyGate:
    held = {f"header {name}": value for name, value in (headers or {}).items()}
    return SafetyGate.from_environment(extra={**secret_items(), **held})


def _reloader(headers: Mapping[str, str] | None) -> Callable[[], SafetyGate | None]:
    """A reload for the gate: a fresh gate once the default ``SecretManager`` is another one than
    the last seen (the engine's log redactor watches it the same way), else ``None``."""
    seen = [_default_manager()]

    def reload() -> SafetyGate | None:
        manager = _default_manager()
        if manager is seen[0]:
            return None
        seen[0] = manager
        return _read_gate(headers)

    return reload


def _default_manager() -> Any:
    """The default ``SecretManager`` if one is installed, else ``None``."""
    try:
        from tolokaforge.secrets import get_default_or_none
    except ImportError:  # pragma: no cover - the secrets package is part of core
        return None
    return get_default_or_none()


def secret_items() -> dict[str, str]:
    """The credentials the ``SecretManager`` resolves under secret-like key names, by name."""
    manager = _default_manager()
    if manager is None:
        return {}
    items: dict[str, str] = {}
    for key in manager.list_all_keys():
        if not looks_secret(key):
            continue
        value = manager.get_secret(key)
        if value:
            items[key] = value
    return items


def secret_values() -> list[str]:
    """The credential values the ``SecretManager`` resolves under secret-like key names."""
    return list(secret_items().values())


def otlp_headers() -> dict[str, str] | None:
    """The receiver's OTLP headers, resolved through the shared SecretManager."""
    return _parse_headers(_secret(OTLP_HEADERS_SECRET)) or None
