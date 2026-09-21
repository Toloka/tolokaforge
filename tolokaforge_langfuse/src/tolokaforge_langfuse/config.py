"""Langfuse-owned settings under ``observability.tracing.options.langfuse``.

The engine passes plugin options through unchanged. This model validates the receiver's
configuration before any connection or export is attempted and imports no engine modules.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LangfuseConfig(BaseModel):
    """The receiver-plane contract; transport and run identity remain engine-owned."""

    model_config = {"extra": "forbid"}

    expect_project: str | None = None
    """Project name returned by ``GET /api/public/projects``; a mismatch refuses tracing.
    Defaults to ``TOLOKAFORGE_TRACING_EXPECT_PROJECT`` or ``LANGFUSE_PROJECT``."""
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
    profile: str | None = None
    """Deployment-profile TOML path; defaults to ``TOLOKAFORGE_TRACING_PROFILE``."""
    environment: str | None = None
    """Native environment, overriding the profile; ``LANGFUSE_ENVIRONMENT`` wins over both."""
    model_name_normalizer: Literal["none", "toloka"] = "none"
    """Raw provider/model names or facets from the optional toloka-model-name-normalizer."""
    model_name_rules: str | None = None
    """Deployment-specific normalizer rules; profile rules apply when this is unset."""

    @model_validator(mode="after")
    def _check_normalizer(self) -> LangfuseConfig:
        if self.model_name_rules and self.model_name_normalizer == "none":
            raise ValueError("model_name_rules requires model_name_normalizer='toloka'.")
        return self
