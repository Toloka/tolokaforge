"""Capability and flag registry for bring-your-own-harness adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


class HarnessCapabilities(BaseModel):
    """Portable features a harness adapter can provide."""

    supports_resume: bool = False
    supports_mcp_headers: bool = False
    supports_atif: bool = False
    requires_interactive_auth: bool = False
    runtime_available: bool = True

    model_config = {"frozen": True, "extra": "forbid"}


class _FlagsModel(BaseModel):
    model_config = {"extra": "forbid"}


class ClaudeCodeFlags(_FlagsModel):
    model: str | None = None
    permission_mode: Literal[
        "acceptEdits", "auto", "manual", "plan", "dontAsk", "bypassPermissions"
    ] = "bypassPermissions"
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    append_system_prompt: str | None = None


class CodexFlags(_FlagsModel):
    model: str | None = None
    approval_policy: Literal["untrusted", "on-failure", "on-request", "never"] = "never"
    sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"


class ACPFlags(_FlagsModel):
    command: list[str] = Field(min_length=1)
    cwd: str = "/work"


class DocumentationOnlyFlags(_FlagsModel):
    pass


@dataclass(frozen=True)
class HarnessAdapterSpec:
    """Static adapter contract used by config validation and runtime setup."""

    type: str
    default_version: str | None
    flags_model: type[_FlagsModel]
    capabilities: HarnessCapabilities
    required_secret_keys: tuple[str, ...] = ()
    provider_hosts: tuple[str, ...] = ()
    install_hosts: tuple[str, ...] = ()

    def validate_flags(self, flags: dict[str, Any]) -> dict[str, Any]:
        return self.flags_model.model_validate(flags).model_dump(exclude_none=True)


_SPECS = {
    "claude-code": HarnessAdapterSpec(
        type="claude-code",
        default_version="2.1.203",
        flags_model=ClaudeCodeFlags,
        capabilities=HarnessCapabilities(
            supports_resume=True,
            supports_mcp_headers=True,
            supports_atif=True,
        ),
        required_secret_keys=("ANTHROPIC_API_KEY",),
        provider_hosts=("api.anthropic.com",),
        install_hosts=("registry.npmjs.org",),
    ),
    "codex": HarnessAdapterSpec(
        type="codex",
        default_version=None,
        flags_model=CodexFlags,
        capabilities=HarnessCapabilities(
            supports_resume=True,
            supports_mcp_headers=True,
            supports_atif=True,
        ),
        required_secret_keys=("OPENAI_API_KEY",),
        provider_hosts=("api.openai.com",),
    ),
    "acp": HarnessAdapterSpec(
        type="acp",
        default_version=None,
        flags_model=ACPFlags,
        capabilities=HarnessCapabilities(supports_atif=True),
    ),
    "cursor": HarnessAdapterSpec(
        type="cursor",
        default_version=None,
        flags_model=DocumentationOnlyFlags,
        capabilities=HarnessCapabilities(
            requires_interactive_auth=True,
            runtime_available=False,
        ),
    ),
    "copilot": HarnessAdapterSpec(
        type="copilot",
        default_version=None,
        flags_model=DocumentationOnlyFlags,
        capabilities=HarnessCapabilities(
            requires_interactive_auth=True,
            runtime_available=False,
        ),
    ),
    "gemini": HarnessAdapterSpec(
        type="gemini",
        default_version=None,
        flags_model=DocumentationOnlyFlags,
        capabilities=HarnessCapabilities(
            requires_interactive_auth=True,
            runtime_available=False,
        ),
    ),
    "opencode": HarnessAdapterSpec(
        type="opencode",
        default_version=None,
        flags_model=DocumentationOnlyFlags,
        capabilities=HarnessCapabilities(
            requires_interactive_auth=True,
            runtime_available=False,
        ),
    ),
}


def get_harness_spec(harness_type: str) -> HarnessAdapterSpec:
    """Return one registered harness contract or fail with available names."""

    try:
        return _SPECS[harness_type]
    except KeyError as exc:
        available = ", ".join(sorted(_SPECS))
        raise ValueError(
            f"Unknown agent harness {harness_type!r}; available adapters: {available}"
        ) from exc


def list_harness_specs() -> tuple[HarnessAdapterSpec, ...]:
    """Return adapter metadata in stable type order."""

    return tuple(_SPECS[name] for name in sorted(_SPECS))
