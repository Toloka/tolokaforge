"""Unit tests for :class:`SharedStackRuntimeBackend`'s env_manifest mode.

Covers the composer-driven wiring: when ``env_manifest`` is set at
construction, the backend delegates run-scope materialisation to the
injected :class:`SubstrateComposer` at ``connect`` time, resolves
endpoints and runner client from the returned :class:`RunSubstrate`,
and hands the substrate back to the composer at ``close``. Built-in-
stack mode (``runner_address`` + ``endpoints`` injected at
construction) is unchanged and covered by the existing test suite.

The composer's docker-side wiring — compose materialisation, network
policy, credential injection, log routing, failure-time capture — is
locked by the materialiser and composer tests directly; the backend
tests below assert only on the seam.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.composition_runtime import RunSubstrate
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest

pytestmark = pytest.mark.unit


def _make_manifest(tmp_path: Path) -> EnvironmentManifest:
    """Author a minimal valid manifest whose compose file exists on disk.

    ``EnvironmentManifest`` validates that ``compose_file`` exists and
    parses as a YAML mapping; the fixture here writes a two-service stub
    (runner + db-service) that satisfies the manifest validator without
    needing a real docker daemon."""
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  runner:\n"
        "    image: tolokaforge-runner:local\n"
        "    ports:\n"
        '      - "50051"\n'
        "  db-service:\n"
        "    image: tolokaforge-db-service:local\n"
        "    ports:\n"
        '      - "8000"\n'
    )
    return EnvironmentManifest(compose_file=compose_file, runner_service="runner")


def _stub_run_substrate(
    *,
    runner_client: MagicMock | None = None,
    endpoints: EnvEndpoints | None = None,
    run_id: str = "run-x",
) -> RunSubstrate:
    """Assemble a :class:`RunSubstrate` a stub composer returns from ``materialise_run``."""
    if endpoints is None:
        endpoints = EnvEndpoints(
            db_url="http://localhost:65432",
            rag_url=None,
            runner_url="http://localhost:60051",
        )
    return RunSubstrate(
        run_id=run_id,
        run_stack_handles=(),
        task_stack_handles={},
        runner_client=runner_client,
        endpoints=endpoints,
        seeds={},
        mount_docker_socket=False,
        log_capture=None,
        events=MagicMock(),
    )


class TestBackwardCompatibility:
    """The built-in-stack mode (no env_manifest) must be unchanged."""

    def test_default_construction_still_works(self) -> None:
        backend = SharedStackRuntimeBackend()
        assert backend.runner_client is not None
        assert backend._endpoints is not None
        assert backend._env_manifest is None
        assert backend._run_substrate is None

    def test_endpoints_injection_still_works(self) -> None:
        endpoints = EnvEndpoints(
            db_url="http://db.example:8000",
            rag_url=None,
            runner_url="http://runner.example:50051",
        )
        backend = SharedStackRuntimeBackend(
            runner_address="runner.example:50051", endpoints=endpoints
        )
        assert backend._endpoints is endpoints


class TestEnvManifestConstruction:
    """Construction-time invariants when ``env_manifest`` is set."""

    def test_env_manifest_mode_defers_client_and_endpoints(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="my-run")

        assert backend._env_manifest is manifest
        assert backend._run_id == "my-run"
        assert backend.runner_client is None
        assert backend._endpoints is None
        assert backend._run_substrate is None
        assert backend._env_handles == {}
        assert backend._connected_trials == set()

    def test_env_manifest_plus_endpoints_raises(self, tmp_path: Path) -> None:
        """The two modes are mutually exclusive — passing both is a
        contract violation, fail loud instead of silently ignoring one."""
        manifest = _make_manifest(tmp_path)
        endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
        with pytest.raises(ValueError, match="env_manifest OR endpoints"):
            SharedStackRuntimeBackend(env_manifest=manifest, endpoints=endpoints)

    def test_default_composer_is_default_substrate_composer(self, tmp_path: Path) -> None:
        """Composer defaults to :class:`DefaultSubstrateComposer` when not
        injected — the injection seam is optional, the built-in composer
        wired at construction time is the production path."""
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        manifest = _make_manifest(tmp_path)
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="my-run")
        assert isinstance(backend.composer, DefaultSubstrateComposer)


class TestConnectMaterialises:
    """``connect`` materialises the run via the composer and connects the
    resolved runner client. The composer is the sole seam; docker-side
    wiring is exercised by the composer + materialiser tests, not here.
    """

    def test_connect_delegates_run_materialisation_to_composer(self, tmp_path: Path) -> None:
        """``connect()`` builds a :class:`RunCtx` from the backend's
        construction args, hands the manifest plan to
        :meth:`SubstrateComposer.materialise_run`, and stores the
        returned :class:`RunSubstrate` on the backend."""
        manifest = _make_manifest(tmp_path)
        stub_client = MagicMock()
        stub_sub = _stub_run_substrate(runner_client=stub_client)
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub

        backend = SharedStackRuntimeBackend(
            env_manifest=manifest,
            run_id="run-x",
            composer=composer,
        )
        backend.connect(timeout=5.0, retry_interval=0.1)

        composer.materialise_run.assert_called_once()
        call = composer.materialise_run.call_args
        assert call.kwargs["plan"] == list(manifest.stacks)
        ctx = call.kwargs["ctx"]
        assert ctx.run_id == "run-x"
        assert ctx.manifest is manifest
        assert ctx.mount_docker_socket is False
        assert ctx.log_capture is None
        assert ctx.seeds == {}
        assert backend._run_substrate is stub_sub
        stub_client.connect.assert_called_once_with(timeout=5.0, retry_interval=0.1)

    def test_connect_skips_client_connect_when_runner_not_owned_by_run_scope(
        self, tmp_path: Path
    ) -> None:
        """A TRIAL_SCOPED_ONLY plan returns a :class:`RunSubstrate` with
        ``runner_client=None``; the run-scope :meth:`connect` must skip
        client-side connect (a trial-scope stack will bring the runner
        up per trial)."""
        manifest = _make_manifest(tmp_path)
        stub_sub = _stub_run_substrate(runner_client=None, endpoints=None)
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()

        assert backend._run_substrate is stub_sub

    def test_connect_surfaces_composer_provision_error(self, tmp_path: Path) -> None:
        """A composer refusal (e.g. INV-12 violation, docker start
        failure) reaches the backend as a :class:`ProvisionError` and
        leaves ``_run_substrate`` unset — the composer owns rollback."""
        manifest = _make_manifest(tmp_path)
        composer = MagicMock()
        composer.materialise_run.side_effect = ProvisionError(
            trial_id="run-x",
            stage="materialise_run",
            reason="docker compose up failed",
        )

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        with pytest.raises(ProvisionError, match="docker compose up failed"):
            backend.connect()

        assert backend._run_substrate is None

    def test_connect_accepts_db_url_none(self, tmp_path: Path) -> None:
        """``db_url`` is best-effort — a manifest whose compose file
        omits ``db-service:8000`` reaches the backend as
        ``EnvEndpoints(db_url=None, ...)`` and is surfaced verbatim by
        :meth:`endpoints`. Fixture pinned so a regression that silently
        substitutes a placeholder db_url fails loud."""
        manifest = _make_manifest(tmp_path)
        stub_client = MagicMock()
        stub_sub = _stub_run_substrate(
            runner_client=stub_client,
            endpoints=EnvEndpoints(db_url=None, rag_url=None, runner_url="http://localhost:60051"),
        )
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()

        assert backend._run_substrate is stub_sub
        assert backend._run_substrate.endpoints is not None
        assert backend._run_substrate.endpoints.db_url is None


class TestCloseIdempotency:
    """``close`` is safe to call in any state (before materialisation,
    after materialisation, twice) — the run lifecycle."""

    def test_close_without_materialisation_is_noop(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        composer = MagicMock()
        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)

        backend.close()  # must not raise

        composer.teardown_run.assert_not_called()

    def test_close_teardown_delegates_to_composer(self, tmp_path: Path) -> None:
        """After a successful materialisation, ``close`` hands the
        substrate to :meth:`SubstrateComposer.teardown_run` (which owns
        runner-client close AND stack teardown) and clears the backend's
        per-trial caches."""
        manifest = _make_manifest(tmp_path)
        stub_sub = _stub_run_substrate(runner_client=MagicMock())
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend._env_handles["trial-1"] = MagicMock()
        backend._connected_trials.add("trial-1")

        backend.close()

        composer.teardown_run.assert_called_once_with(stub_sub)
        assert backend._run_substrate is None
        assert backend._env_handles == {}
        assert backend._connected_trials == set()

    def test_close_twice_is_idempotent(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        stub_sub = _stub_run_substrate(runner_client=MagicMock())
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()

        backend.close()
        backend.close()  # must not raise

        # teardown fired once (on the first close), not twice
        composer.teardown_run.assert_called_once()

    def test_close_swallows_composer_teardown_errors(self, tmp_path: Path) -> None:
        """A composer teardown failure must not surface past ``close`` —
        the run wrapper is expected to be fail-safe. Backend state is
        still cleared."""
        manifest = _make_manifest(tmp_path)
        stub_sub = _stub_run_substrate(runner_client=MagicMock())
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub
        composer.teardown_run.side_effect = RuntimeError("compose stop failed")

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()

        backend.close()  # must not raise

        assert backend._run_substrate is None


class TestMaterialiseIdempotent:
    """A second ``connect()`` in env_manifest mode must not clobber the
    running stack — the substrate outlives the reconnect."""

    def test_double_connect_does_not_re_materialise(self, tmp_path: Path) -> None:
        manifest = _make_manifest(tmp_path)
        stub_client = MagicMock()
        stub_sub = _stub_run_substrate(runner_client=stub_client)
        composer = MagicMock()
        composer.materialise_run.return_value = stub_sub

        backend = SharedStackRuntimeBackend(env_manifest=manifest, composer=composer)
        backend.connect()
        backend.connect()  # second call must be a no-op

        composer.materialise_run.assert_called_once()
        # Client connect fired once on the first materialise; the second
        # connect is a plain early-return.
        stub_client.connect.assert_called_once()
