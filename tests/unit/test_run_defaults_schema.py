"""Unit tests for RunDefaults + Compute/Storage/Observability schemas.

Stage-1 scope: models only. The loader that consumes these lands in
the next milestone. See docs/architecture/PROJECTS.md.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import (
    ComputeConfig,
    EngineConfig,
    EvaluationConfig,
    LocalDockerComputeConfig,
    LocalStorageConfig,
    LoggingConfig,
    MetricsConfig,
    ModelConfig,
    ObservabilityConfig,
    OrchestratorConfig,
    QueueStorageConfig,
    RunConfig,
    RunDefaults,
    S3StorageConfig,
    StorageConfig,
    TracingConfig,
)

pytestmark = pytest.mark.unit


class TestComputeConfig:
    def test_default_provider_is_local_docker(self) -> None:
        c = ComputeConfig()
        assert c.provider == "local-docker"
        assert c.workers is None
        assert c.max_budget_usd is None
        assert c.max_requests_per_second is None
        assert c.max_attempt_retries is None
        assert c.local_docker is None

    def test_rejects_unknown_provider(self) -> None:
        with pytest.raises(ValidationError):
            ComputeConfig(provider="kubernetes")  # type: ignore[arg-type]

    def test_workers_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ComputeConfig(workers=0)

    def test_budget_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ComputeConfig(max_budget_usd=-1.0)
        ComputeConfig(max_budget_usd=0.0)  # zero is allowed

    def test_rate_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ComputeConfig(max_requests_per_second=0.0)

    def test_retries_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ComputeConfig(max_attempt_retries=-1)
        ComputeConfig(max_attempt_retries=0)  # zero is allowed

    def test_provider_sub_block_binds(self) -> None:
        c = ComputeConfig(local_docker=LocalDockerComputeConfig())
        assert c.local_docker is not None


class TestStorageConfig:
    def test_defaults_all_none(self) -> None:
        s = StorageConfig()
        assert s.artifacts is None
        assert s.logs is None
        assert s.queue is None

    def test_local_artifacts(self) -> None:
        s = StorageConfig(artifacts=LocalStorageConfig(path="./results"))
        assert s.artifacts is not None
        assert s.artifacts.type == "local"
        assert s.artifacts.path == "./results"

    def test_s3_artifacts(self) -> None:
        s = StorageConfig(artifacts=S3StorageConfig(bucket="team-toloka", prefix="nightly"))
        assert s.artifacts is not None
        assert s.artifacts.type == "s3"
        assert s.artifacts.bucket == "team-toloka"
        assert s.artifacts.prefix == "nightly"

    def test_queue_defaults(self) -> None:
        q = QueueStorageConfig()
        assert q.backend == "sqlite"
        assert q.postgres_dsn is None

    def test_queue_postgres(self) -> None:
        q = QueueStorageConfig(backend="postgres", postgres_dsn="postgresql://x@h/db")
        assert q.backend == "postgres"
        assert q.postgres_dsn == "postgresql://x@h/db"

    def test_queue_rejects_unknown_backend(self) -> None:
        with pytest.raises(ValidationError):
            QueueStorageConfig(backend="redis")  # type: ignore[arg-type]


class TestObservabilityConfig:
    def test_defaults_all_none(self) -> None:
        o = ObservabilityConfig()
        assert o.tracing is None
        assert o.metrics is None
        assert o.logging is None

    def test_tracing_defaults_to_none_exporter(self) -> None:
        t = TracingConfig()
        assert t.exporter == "none"
        assert t.endpoint is None

    def test_tracing_otlp(self) -> None:
        t = TracingConfig(exporter="otlp", endpoint="http://collector:4317")
        assert t.exporter == "otlp"
        assert t.endpoint == "http://collector:4317"

    def test_metrics_prometheus(self) -> None:
        m = MetricsConfig(exporter="prometheus", endpoint="http://prom:9090")
        assert m.exporter == "prometheus"

    def test_logging_defaults(self) -> None:
        lg = LoggingConfig()
        assert lg.level == "INFO"
        assert lg.exporter == "stdout"
        assert lg.endpoint is None

    def test_logging_level_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            LoggingConfig(level="VERBOSE")  # type: ignore[arg-type]


class TestRunDefaults:
    def test_all_fields_optional(self) -> None:
        r = RunDefaults()
        assert r.compute is None
        assert r.storage is None
        assert r.observability is None
        assert r.orchestrator is None
        assert r.models == {}

    def test_populated(self) -> None:
        r = RunDefaults(
            compute=ComputeConfig(workers=4, max_budget_usd=20.0),
            storage=StorageConfig(artifacts=LocalStorageConfig(path="./out")),
            observability=ObservabilityConfig(logging=LoggingConfig(level="DEBUG")),
        )
        assert r.compute is not None and r.compute.workers == 4
        assert r.storage is not None and r.storage.artifacts is not None
        assert r.observability is not None and r.observability.logging is not None


class TestRunConfigNewFields:
    """RunConfig gets three optional new blocks; unset means None so
    every existing pack loads unchanged."""

    def _minimal_kwargs(self) -> dict[str, object]:
        return {
            "models": {
                "agent": ModelConfig(provider="openrouter", name="test/model"),
                "user": ModelConfig(provider="openrouter", name="test/model"),
                "judge": ModelConfig(provider="openrouter", name="test/model"),
            },
            "orchestrator": OrchestratorConfig(),
            "evaluation": EvaluationConfig(output_dir="results/x"),
        }

    def test_new_blocks_default_to_none(self) -> None:
        rc = RunConfig(**self._minimal_kwargs())
        assert rc.compute is None
        assert rc.storage is None
        assert rc.observability is None

    def test_engine_still_optional(self) -> None:
        rc = RunConfig(**self._minimal_kwargs())
        assert rc.engine is None

    def test_new_blocks_bind_when_provided(self) -> None:
        rc = RunConfig(
            **self._minimal_kwargs(),
            compute=ComputeConfig(workers=8),
            storage=StorageConfig(
                queue=QueueStorageConfig(backend="sqlite"),
            ),
            observability=ObservabilityConfig(tracing=TracingConfig(exporter="none")),
        )
        assert rc.compute is not None and rc.compute.workers == 8
        assert rc.storage is not None and rc.storage.queue is not None
        assert rc.observability is not None and rc.observability.tracing is not None

    def test_engine_config_still_binds(self) -> None:
        rc = RunConfig(
            **self._minimal_kwargs(),
            engine=EngineConfig(presets_file="/tmp/x.yaml"),
        )
        assert rc.engine is not None
        assert rc.engine.presets_file == "/tmp/x.yaml"
