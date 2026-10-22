"""``docker.services``: which optional engine services a run starts (#1937).

The field (defaults, valid and invalid values, the YAML boolean trap,
immutability) and its parse through ``RunConfig``.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.core.models.docker_config import DockerConfig, EngineServicesConfig
from tolokaforge.core.models.run_config import RunConfig

pytestmark = pytest.mark.unit


def _run_config(docker: object) -> RunConfig:
    return RunConfig.model_validate(
        {
            "models": {},
            "orchestrator": {},
            "evaluation": {"task_packs": [], "output_dir": "/tmp/tolokaforge-test-out"},
            "docker": docker,
        }
    )


class TestEngineServicesConfig:
    def test_every_service_defaults_to_auto(self) -> None:
        services = DockerConfig().services
        assert (services.rag_service, services.mock_web) == ("auto", "auto")

    @pytest.mark.parametrize("mode", ["auto", "always", "never"])
    def test_accepts_the_three_modes(self, mode: str) -> None:
        services = EngineServicesConfig(rag_service=mode, mock_web=mode)  # type: ignore[arg-type]
        assert (services.rag_service, services.mock_web) == (mode, mode)

    def test_rejects_an_unknown_mode(self) -> None:
        with pytest.raises(ValidationError):
            EngineServicesConfig(rag_service="sometimes")  # type: ignore[arg-type]

    def test_rejects_an_unknown_service(self) -> None:
        with pytest.raises(ValidationError):
            EngineServicesConfig.model_validate({"typesense": "always"})

    @pytest.mark.parametrize("spelling", ["off", "on", "yes", "false"])
    def test_names_the_yaml_boolean_trap(self, spelling: str) -> None:
        """``on`` / ``off`` read as booleans; the error says so instead of listing modes."""
        block = yaml.safe_load(f"rag_service: {spelling}")
        with pytest.raises(ValidationError, match="YAML reads on/off"):
            EngineServicesConfig.model_validate(block)

    def test_is_frozen(self) -> None:
        services = EngineServicesConfig()
        with pytest.raises(ValidationError):
            services.rag_service = "never"  # type: ignore[misc]


class TestTheRunConfigBlock:
    def test_parses_from_the_docker_block(self) -> None:
        config = _run_config({"services": {"rag_service": "never", "mock_web": "always"}})
        assert config.docker is not None
        assert config.docker.services == EngineServicesConfig(
            rag_service="never", mock_web="always"
        )

    def test_a_docker_block_without_services_resolves_every_service_automatically(self) -> None:
        config = _run_config({"image_source": "pull"})
        assert config.docker is not None
        assert config.docker.services == EngineServicesConfig()
