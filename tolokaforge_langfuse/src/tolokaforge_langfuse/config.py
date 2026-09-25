"""Langfuse-owned settings under ``observability.tracing.options.langfuse``.

The engine passes plugin options through unchanged. This model validates the receiver's
configuration before any connection or export is attempted and imports no engine modules; the
offline connector reads the same block through :mod:`tolokaforge_langfuse.preflight`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tolokaforge_langfuse.profile import (
    TracingProfileError,
    check_environment,
    profile_from_mapping,
)

ACCEPTS_TRIAL = "trial"
ACCEPTS_TRANSCRIPT = "transcript"
ACCEPTS_ANY = "any"
Accepts = Literal["trial", "transcript", "any"]


class EnvironmentEntry(BaseModel):
    """One native environment of the project, and which trace sources may land in it."""

    model_config = {"extra": "forbid", "frozen": True}

    accepts: tuple[Accepts, ...] = Field(min_length=1)
    """``trial`` (benchmark data), ``transcript`` (an agent's own transcript) or ``any``."""

    def admits(self, source: str) -> bool:
        """Whether a trace whose ``accepts`` token is ``source`` may land here."""
        return ACCEPTS_ANY in self.accepts or source in self.accepts


class LangfuseConfig(BaseModel):
    """The receiver-plane contract; transport and run identity remain engine-owned."""

    model_config = {"extra": "forbid"}

    expect_project: str | None = None
    """Project name returned by ``GET /api/public/projects``; a mismatch refuses tracing.
    Defaults to ``project``, then ``TOLOKAFORGE_TRACING_EXPECT_PROJECT`` or ``LANGFUSE_PROJECT``."""
    project: str | None = None
    """The one receiver project of the deployment; the credentials must open it."""
    project_id: str | None = None
    """That project's id, for the launchers that can compare it (``GET /api/public/projects``)."""
    environments: dict[str, EnvironmentEntry] | None = None
    """The project's native environments; when declared, ``LANGFUSE_ENVIRONMENT`` must name one
    whose ``accepts`` admits the trace's source."""
    attach: Literal["all", "core", "none"] = "all"
    """Files uploaded through the media API after the trial bundle is persisted."""
    gradings: bool = True
    """Include the grading observation, its judge transcript and scores at trial end."""
    projection: Literal["full", "gradings", "none"] = "full"
    """Persisted-bundle records sent through ingestion, in addition to attachments."""
    server_api: Literal["auto", "v3", "v4"] = "auto"
    """Which receiver family to write for; ``auto`` asks the receiver once, at run start
    (``GET /api/public/v2/observations``: a v4 server answers it in every write mode, a v3 one
    404s). ``v4`` writes every observation once, the live rows as declared previews."""
    attach_api_base: str | None = None
    """REST base URL; by default strip ``/api/public/otel/v1/traces`` from the endpoint."""
    attach_timeout_s: float = Field(default=60.0, gt=0)
    """Per-request timeout of the attachment step and project check."""
    attach_budget_s: float = Field(default=120.0, gt=0)
    """Whole-trial attachment budget; three consecutive unreachable trials open the breaker."""
    profile: str | dict[str, Any] | None = None
    """The deployment profile: a TOML or YAML path, or the profile itself inline; defaults to
    ``TOLOKAFORGE_TRACING_PROFILE``. Relative paths anchor to the supplying ``project.yaml``."""
    environment: str | None = None
    """Native environment, overriding the profile; ``LANGFUSE_ENVIRONMENT`` wins over both.
    Not allowed next to ``environments``, where the variable is the only selector."""
    model_name_normalizer: Literal["none", "toloka"] = "none"
    """Raw provider/model names or facets from the optional toloka-model-name-normalizer."""
    model_name_rules: str | None = None
    """Deployment-specific normalizer rules; profile rules apply when this is unset."""

    @field_validator("environments")
    @classmethod
    def _check_environments(
        cls, value: dict[str, EnvironmentEntry] | None
    ) -> dict[str, EnvironmentEntry] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("environments must declare at least one environment")
        for name in value:
            try:
                check_environment(name, where="environments")
            except TracingProfileError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("profile")
    @classmethod
    def _check_inline_profile(cls, value: str | dict[str, Any] | None) -> Any:
        if isinstance(value, dict):
            # the shape only: paths inside anchor to the supplying file, checked when resolved
            try:
                profile_from_mapping(value, check_files=False)
            except TracingProfileError as exc:
                raise ValueError(str(exc)) from exc
        elif isinstance(value, str) and not value.strip():
            raise ValueError("profile must be a non-empty path or an inline profile")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> LangfuseConfig:
        if self.model_name_rules and self.model_name_normalizer == "none":
            raise ValueError("model_name_rules requires model_name_normalizer='toloka'.")
        if self.expect_project and self.project and self.expect_project != self.project:
            raise ValueError(
                f"expect_project={self.expect_project!r} contradicts project={self.project!r}"
            )
        if self.environments is not None and self.environment is not None:
            raise ValueError(
                "environment and environments exclude each other: with environments declared,"
                " LANGFUSE_ENVIRONMENT selects one of them"
            )
        if self.project_id is not None and self.project is None:
            raise ValueError("project_id needs project")
        return self

    def expected_project(self) -> str | None:
        """The project the credentials must open, as far as this block says."""
        return self.expect_project or self.project
