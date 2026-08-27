"""Guard: `TypeSenseConfig` refuses `mode="remote" + port="auto"` at parse time.

The `"auto"` sentinel is meaningful only for `mode="local"` (auto-allocate a
local server port). Nothing resolves it for `mode="remote"`, and the downstream
`int(port)` cast in the adapter surface crashes on the string. The validator
moves the refusal from stack-build time (where it already raises) to config
parse time (fail-fast).
"""

from __future__ import annotations

import textwrap

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.core.models.run_config import TypeSenseConfig

pytestmark = pytest.mark.unit


def test_remote_with_auto_port_refused_at_parse_time() -> None:
    with pytest.raises(ValidationError) as exc_info:
        TypeSenseConfig(enabled=True, mode="remote", host="ts.example", port="auto")

    message = str(exc_info.value)
    assert 'mode="remote" requires a concrete port' in message


def test_local_with_auto_port_parses() -> None:
    """`mode="local" + port="auto"` is the default configuration; it must
    continue to parse so a future maintainer does not over-tighten the
    validator."""
    config = TypeSenseConfig(enabled=True, mode="local", port="auto")

    assert config.mode == "local"
    assert config.port == "auto"


def test_remote_with_concrete_port_parses() -> None:
    config = TypeSenseConfig(enabled=True, mode="remote", host="ts.example", port=443)

    assert config.mode == "remote"
    assert config.port == 443


def test_disabled_enabled_false_with_auto_port_parses() -> None:
    """`enabled=False` and `mode="disabled"` render the port inert
    (`effective_typesense()` returns `None`); coercing the sentinel there
    would be mute-error territory. Leave it alone."""
    config_disabled_flag = TypeSenseConfig(enabled=False, port="auto")
    assert config_disabled_flag.port == "auto"

    config_disabled_mode = TypeSenseConfig(enabled=True, mode="disabled", port="auto")
    assert config_disabled_mode.mode == "disabled"
    assert config_disabled_mode.port == "auto"


def test_yaml_load_of_invalid_combo_raises_at_parse_time() -> None:
    """YAML is the primary loading path for run configs — the validator must
    fire there too, not just on kwargs construction."""
    yaml_text = textwrap.dedent("""
        enabled: true
        mode: remote
        host: ts.example
        port: auto
        """)
    payload = yaml.safe_load(yaml_text)

    with pytest.raises(ValidationError) as exc_info:
        TypeSenseConfig.model_validate(payload)

    assert 'mode="remote" requires a concrete port' in str(exc_info.value)
