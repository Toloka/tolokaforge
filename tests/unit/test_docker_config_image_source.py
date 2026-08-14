"""Unit tests for ``DockerConfig.image_source`` and its wiring into ``RunConfig``.

Covers the config-schema surface for #1068 — the field itself
(defaults, valid values, invalid values, immutability) and its
integration into ``RunConfig`` (declared here as a first-party
sub-block, not an ignored extra).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.models.docker_config import DockerConfig
from tolokaforge.core.models.run_config import RunConfig

pytestmark = pytest.mark.unit


def _minimal_run_config_data(**overrides: object) -> dict[str, object]:
    """Smallest ``RunConfig`` payload that parses, with room for overrides."""
    data: dict[str, object] = {
        "models": {},
        "orchestrator": {},
        "evaluation": {"task_packs": [], "output_dir": "/tmp/tolokaforge-test-out"},
    }
    data.update(overrides)
    return data


class TestDockerConfigImageSourceField:
    def test_defaults_to_auto(self) -> None:
        cfg = DockerConfig()
        assert cfg.image_source == "auto"

    @pytest.mark.parametrize("value", ["auto", "pull", "build"])
    def test_accepts_all_three_values(self, value: str) -> None:
        cfg = DockerConfig(image_source=value)  # type: ignore[arg-type]
        assert cfg.image_source == value

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            DockerConfig(image_source="pulll")  # type: ignore[arg-type]
        assert "image_source" in str(excinfo.value)

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            DockerConfig(image_source="")  # type: ignore[arg-type]

    def test_field_is_immutable_when_frozen(self) -> None:
        cfg = DockerConfig(image_source="pull")
        with pytest.raises(ValidationError):
            cfg.image_source = "build"  # type: ignore[misc]

    def test_extra_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DockerConfig(image_source="auto", unknown_field=1)  # type: ignore[call-arg]


class TestRunConfigDockerBlock:
    def test_docker_absent_stays_none(self) -> None:
        run = RunConfig(**_minimal_run_config_data())  # type: ignore[arg-type]
        assert run.docker is None

    def test_docker_block_parses_to_docker_config(self) -> None:
        run = RunConfig(
            **_minimal_run_config_data(docker={"image_source": "pull"})  # type: ignore[arg-type]
        )
        assert run.docker is not None
        assert isinstance(run.docker, DockerConfig)
        assert run.docker.image_source == "pull"

    def test_docker_block_defaults_when_empty(self) -> None:
        run = RunConfig(**_minimal_run_config_data(docker={}))  # type: ignore[arg-type]
        assert run.docker is not None
        assert run.docker.image_source == "auto"

    def test_docker_block_rejects_invalid_image_source(self) -> None:
        with pytest.raises(ValidationError):
            RunConfig(
                **_minimal_run_config_data(docker={"image_source": "unknown"})  # type: ignore[arg-type]
            )

    def test_docker_block_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            RunConfig(
                **_minimal_run_config_data(docker={"image_source": "auto", "nope": 1})  # type: ignore[arg-type]
            )
