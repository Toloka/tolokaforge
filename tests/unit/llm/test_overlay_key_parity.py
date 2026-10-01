"""`config validate` and the run find the same `litellm_models` entry for a config."""

from __future__ import annotations

import logging
import pickle
import uuid
from pathlib import Path

import litellm
import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.core.config_validator import Severity, validate_run_config
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.litellm_params import OverlayKeyMismatchError, lookup_overlay
from tolokaforge.core.llm.presets import set_overlay_path
from tolokaforge.core.llm.providers import provider_binding_names
from tolokaforge.core.llm.proxy import ProxyConfigError
from tolokaforge.core.llm.session_header import SessionHeaderConflictError
from tolokaforge.core.models.run_config import ModelConfig, iter_model_configs
from tolokaforge.dx.cli.main import cli
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit

EVIDENCE = "2026-09-30, litellm 1.93.0: no entry, so the route refused tools before sending"

#: `(provider, name)` as a config states them. The names are absent from
#: litellm's map by construction; the first two carry a `/` that is not the
#: provider.
CONFIGS = [
    ("openai", "self-hosted/tolokaforge-canary-a"),
    ("openrouter", "fake-vendor-xyz/tolokaforge-canary-b"),
    ("meta", "tolokaforge-canary-c"),
    ("nova", "tolokaforge-canary-d"),
]


def _write_overlay(tmp_path: Path, keys: list[str]) -> Path:
    path = tmp_path / "overlay.yaml"
    entries = {key: {"supports_function_calling": True, "evidence": EVIDENCE} for key in keys}
    path.write_text(yaml.safe_dump({"litellm_models": entries}))
    return path


def _overlay(tmp_path: Path) -> Path:
    return _write_overlay(tmp_path, [f"{provider}/{name}" for provider, name in CONFIGS])


def _run_config(provider: str, name: str, overlay: Path | None = None) -> dict:
    return _run_config_for(
        {
            "agent": {"provider": provider, "name": name},
            "user": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"},
        },
        overlay,
    )


def _run_config_for(models: dict, overlay: Path | None = None) -> dict:
    raw: dict = {
        "evaluation": {
            "tasks_glob": "tasks/**/task.yaml",
            "output_dir": "output",
            "harness_adapter": {"type": "frozen_mcp_core"},
        },
        "orchestrator": {"workers": 1, "repeats": 1},
        "models": models,
    }
    if overlay is not None:
        raw["engine"] = {"presets_file": str(overlay)}
    return raw


def _write_run_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize("provider, name", CONFIGS)
def test_the_entry_keyed_provider_slash_name_satisfies_the_preflight_and_the_run(
    provider, name, tmp_path
):
    overlay = _overlay(tmp_path)
    config = _write_run_config(tmp_path, _run_config(provider, name, overlay))

    result = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])

    assert "Preset overlay OK" in result.output, result.output
    assert "cannot confirm function-calling support" not in result.output, result.output
    assert "does not appear to support function calling" not in result.output, result.output

    set_overlay_path(str(overlay))
    client = LLMClient(ModelConfig(provider=provider, name=name))
    assert client.allowed_openai_params == ["tools", "tool_choice", "parallel_tool_calls"]


def test_constructing_a_client_against_an_entry_logs_its_evidence(tmp_path, caplog):
    provider, name = "openai", f"self-hosted/tolokaforge-canary-{uuid.uuid4().hex}"
    set_overlay_path(str(_write_overlay(tmp_path, [f"{provider}/{name}"])))

    with caplog.at_level(logging.INFO, logger="tolokaforge.core.llm.litellm_params"):
        LLMClient(ModelConfig(provider=provider, name=name))

    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Admitting")] == [
        "Admitting tools, tool_choice, parallel_tool_calls for "
        f"{provider}/{name}, which litellm's map does not carry. {EVIDENCE}"
    ]


@pytest.mark.parametrize(
    "provider, name, key",
    [
        ("openai", "self-hosted/tolokaforge-canary-a", "openai/self-hosted/tolokaforge-canary-a"),
        (
            "openrouter",
            "openrouter/fake-vendor-xyz/tolokaforge-canary-b",
            "openrouter/fake-vendor-xyz/tolokaforge-canary-b",
        ),
        ("Meta", "tolokaforge-canary-c", "meta/tolokaforge-canary-c"),
        ("nova", "tolokaforge-canary-d", "nova/tolokaforge-canary-d"),
    ],
)
def test_the_undeclared_hint_names_the_key_the_lookup_uses(provider, name, key):
    set_overlay_path(None)

    result = validate_run_config(_run_config(provider, name))

    hints = [
        i.hint
        for i in result.issues
        if i.severity == Severity.INFO
        and i.path == "models.agent.name"
        and "cannot confirm function-calling support" in i.message
    ]
    assert hints == [
        "If the run needs tools, declare it in the presets overlay: "
        f"litellm_models.{key} with supports_function_calling: true"
    ]


GATEWAY = ("openai", "self-hosted/tolokaforge-canary")
NATIVE_AGENT = {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"}


def _as_config(provider: str, name: str, **extra) -> dict:
    return {"provider": provider, "name": name, **extra}


#: `(models block, config path, provider, name)`: one entry under the raw
#: name, placed on every kind of model config the run can build a client for.
#: `openrouter/google/gemini-2.5-pro` is in litellm's map, so the agent's
#: function-calling check never needs the overlay for it.
RAW_NAME_PLACEMENTS = [
    pytest.param(
        {"agent": _as_config(*GATEWAY), "user": NATIVE_AGENT},
        "models.agent",
        *GATEWAY,
        id="agent",
    ),
    pytest.param(
        {"agent": NATIVE_AGENT, "user": _as_config(*GATEWAY)},
        "models.user",
        *GATEWAY,
        id="user",
    ),
    pytest.param(
        {"agent": {**NATIVE_AGENT, "fallbacks": [_as_config(*GATEWAY)]}, "user": NATIVE_AGENT},
        "models.agent.fallbacks[0]",
        *GATEWAY,
        id="agent-fallback",
    ),
    pytest.param(
        {"agent": _as_config("openrouter", "google/gemini-2.5-pro"), "user": NATIVE_AGENT},
        "models.agent",
        "openrouter",
        "google/gemini-2.5-pro",
        id="litellm-mapped-agent",
    ),
]


@pytest.mark.parametrize("models, path, provider, name", RAW_NAME_PLACEMENTS)
def test_an_entry_under_the_raw_name_is_refused_by_validate_and_run_alike(
    models, path, provider, name, tmp_path
):
    raw_key = name
    expected_key = f"{provider}/{name}"
    overlay = _write_overlay(tmp_path, [raw_key])
    config = _write_run_config(tmp_path, _run_config_for(models, overlay))

    set_overlay_path(str(overlay))
    with pytest.raises(OverlayKeyMismatchError) as lookup_error:
        lookup_overlay(provider, name)
    assert (lookup_error.value.declared_key, lookup_error.value.expected_key) == (
        raw_key,
        expected_key,
    )
    assert f"Rename the entry to {expected_key!r}" in str(lookup_error.value)
    with pytest.raises(OverlayKeyMismatchError):
        LLMClient(ModelConfig(provider=provider, name=name))

    validated = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])
    assert validated.exit_code != 0, validated.output
    errors = [line for line in validated.output.splitlines() if "[ERROR]" in line]
    assert len(errors) == 1, validated.output
    assert f"{path}.name:" in errors[0]
    assert repr(raw_key) in errors[0] and repr(expected_key) in errors[0]

    run = CliRunner().invoke(cli, ["run", "--config", str(config), "--dry-run"])
    assert run.exit_code != 0
    assert isinstance(run.exception, OverlayKeyMismatchError), run.output
    assert repr(raw_key) in str(run.exception) and repr(expected_key) in str(run.exception)


def test_an_entry_under_the_raw_name_is_inert_beside_the_canonical_one(tmp_path):
    provider, name = GATEWAY
    overlay = _write_overlay(tmp_path, [name, f"{provider}/{name}"])
    config = _write_run_config(tmp_path, _run_config(provider, name, overlay))

    set_overlay_path(str(overlay))
    assert lookup_overlay(provider, name).params == ("tools", "tool_choice", "parallel_tool_calls")

    validated = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])
    assert "[ERROR]" not in validated.output, validated.output
    run = CliRunner().invoke(cli, ["run", "--config", str(config), "--dry-run"])
    assert not isinstance(run.exception, OverlayKeyMismatchError), run.exception


#: A first segment that names a provider in both sets the refusal consults, in
#: litellm's `provider_list` only, and in `providers.yaml` only.
PROVIDER_VENDORS = ["anthropic", "deepseek", "nova"]


def test_the_provider_vendor_rows_cover_each_membership_the_refusal_consults():
    memberships = [
        (vendor in provider_binding_names(), vendor in litellm.provider_list)
        for vendor in PROVIDER_VENDORS
    ]
    assert memberships == [(True, True), (False, True), (True, False)], (
        "providers.yaml or litellm.provider_list changed: re-pick PROVIDER_VENDORS so the "
        "rows are again one vendor in both sets, one in litellm's only, one in providers.yaml's only"
    )


@pytest.mark.parametrize("vendor", PROVIDER_VENDORS)
def test_a_raw_name_key_that_names_a_provider_is_another_configs_entry(vendor, tmp_path):
    """`<vendor>/…` is the native `(<vendor>, …)` config's own key, so holding
    it while also running `(openrouter, <vendor>/…)` is legitimate: the
    openrouter config is not refused, it is told which config the entry is for."""
    stray = f"{vendor}/tolokaforge-canary"
    overlay = _write_overlay(tmp_path, [stray])
    config = _write_run_config(tmp_path, _run_config("openrouter", stray, overlay))

    set_overlay_path(str(overlay))
    lookup = lookup_overlay("openrouter", stray)
    assert (lookup.params, lookup.stray_key, lookup.stray_provider) == ((), stray, vendor)
    admitted = lookup_overlay(vendor, "tolokaforge-canary")
    assert (admitted.params, admitted.evidence, admitted.stray_key) == (
        ("tools", "tool_choice", "parallel_tool_calls"),
        EVIDENCE,
        None,
    )

    validated = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])
    assert "[ERROR]" not in validated.output, validated.output
    assert (
        f"`{stray}` applies to provider `{vendor}`, "
        f"not to this config, which resolves `openrouter/{stray}`" in validated.output
    ), validated.output


STRAY = ("openrouter", "anthropic/tolokaforge-canary")


@pytest.mark.parametrize(
    "models, path",
    [
        pytest.param(
            {"agent": _as_config(*STRAY), "user": NATIVE_AGENT}, "models.agent", id="agent"
        ),
        pytest.param({"agent": NATIVE_AGENT, "user": _as_config(*STRAY)}, "models.user", id="user"),
        pytest.param(
            {"agent": {**NATIVE_AGENT, "fallbacks": [_as_config(*STRAY)]}, "user": NATIVE_AGENT},
            "models.agent.fallbacks[0]",
            id="agent-fallback",
        ),
    ],
)
def test_validate_names_the_stray_entry_for_every_model_config(models, path, tmp_path):
    _, stray = STRAY
    set_overlay_path(str(_write_overlay(tmp_path, [stray])))

    result = validate_run_config(_run_config_for(models))

    assert [
        (i.severity, i.path) for i in result.issues if f"entry `{stray}` applies to" in i.message
    ] == [(Severity.INFO, f"{path}.name")]


def test_the_refusal_survives_a_pickle_round_trip():
    err = OverlayKeyMismatchError(
        provider="openai",
        name="self-hosted/m",
        declared_key="self-hosted/m",
        expected_key="openai/self-hosted/m",
    )
    back = pickle.loads(pickle.dumps(err))
    assert (
        type(back),
        str(back),
        back.provider,
        back.name,
        back.declared_key,
        back.expected_key,
    ) == (
        OverlayKeyMismatchError,
        str(err),
        "openai",
        "self-hosted/m",
        "self-hosted/m",
        "openai/self-hosted/m",
    )


def test_the_session_header_refusal_survives_a_pickle_round_trip():
    err = SessionHeaderConflictError(
        path="models.agent.fallbacks[0].session.header",
        header="x-session-id",
        source="LLM_PROXY_REQUEST_ID_HEADER",
    )
    back = pickle.loads(pickle.dumps(err))
    assert (type(back), str(back), back.path, back.header, back.source, back.reason) == (
        SessionHeaderConflictError,
        str(err),
        "models.agent.fallbacks[0].session.header",
        "x-session-id",
        "LLM_PROXY_REQUEST_ID_HEADER",
        err.reason,
    )


def _install_gateway_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(secrets_manager, "_default_manager", SecretManager([DictProvider(env)]))


def test_a_session_header_conflict_on_a_fallback_is_refused_by_validate_and_run_alike(
    tmp_path, monkeypatch
):
    """No presets overlay is declared, so the run's refusal cannot come from the
    overlay's own check."""
    _install_gateway_env(
        monkeypatch,
        {
            "LLM_PROXY_BASE_URL": "https://gateway.example.com",
            "LLM_PROXY_REQUEST_ID_HEADER": "X-Session-Id",
        },
    )
    fallback = _as_config(*GATEWAY, session={"header": "x-session-id"})
    raw = _run_config_for(
        {"agent": {**NATIVE_AGENT, "fallbacks": [fallback]}, "user": NATIVE_AGENT}
    )
    assert "engine" not in raw
    config = _write_run_config(tmp_path, raw)

    validated = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])
    assert validated.exit_code != 0, validated.output
    errors = [line for line in validated.output.splitlines() if "[ERROR]" in line]
    assert len(errors) == 1, validated.output
    assert "models.agent.fallbacks[0].session.header:" in errors[0]
    assert "LLM_PROXY_REQUEST_ID_HEADER" in errors[0]

    run = CliRunner().invoke(cli, ["run", "--config", str(config), "--dry-run"])
    assert isinstance(run.exception, SessionHeaderConflictError), run.output
    assert run.exception.path == "models.agent.fallbacks[0].session.header"


@pytest.mark.parametrize(
    "agent_extra, environment_errors, run_refused",
    [({"session": {"header": "x-session-id"}}, 1, True), ({}, 0, False)],
    ids=["declares-session", "no-session"],
)
def test_a_malformed_gateway_environment_concerns_only_configs_that_declare_session(
    tmp_path, monkeypatch, agent_extra, environment_errors, run_refused
):
    """Gateway headers without a gateway base URL are malformed; a config that never
    declares ``session`` does not read them, in validate or at run start."""
    _install_gateway_env(monkeypatch, {"LLM_PROXY_HEADERS": '{"x-team-id": "research"}'})
    raw = _run_config_for({"agent": _as_config(*GATEWAY, **agent_extra), "user": NATIVE_AGENT})
    config = _write_run_config(tmp_path, raw)

    validated = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])
    environment = [
        line
        for line in validated.output.splitlines()
        if "[ERROR]" in line and "(environment)" in line
    ]
    assert len(environment) == environment_errors, validated.output
    assert all("LLM_PROXY_HEADERS" in line for line in environment), validated.output

    run = CliRunner().invoke(cli, ["run", "--config", str(config), "--dry-run"])
    assert isinstance(run.exception, ProxyConfigError) is run_refused, repr(run.exception)


def test_the_walk_reaches_every_fallback_depth_first():
    fallback = ModelConfig(
        provider="openai", name="b", fallbacks=[ModelConfig(provider="openai", name="c")]
    )
    models = {
        "agent": ModelConfig(provider="openai", name="a", fallbacks=[fallback]),
        "user": ModelConfig(provider="openai", name="d"),
    }
    assert [(path, cfg.name) for path, cfg in iter_model_configs(models)] == [
        ("models.agent", "a"),
        ("models.agent.fallbacks[0]", "b"),
        ("models.agent.fallbacks[0].fallbacks[0]", "c"),
        ("models.user", "d"),
    ]
