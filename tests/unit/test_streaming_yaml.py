"""``env.yaml`` is streamed to disk instead of built as one PyYAML node graph.

``yaml.dump`` holds a ``Node`` per scalar of the whole document before it writes
anything, which for the final state of a database-backed world is several times
the state's own size. The streaming writer must read back as the same data, and
with the Python emitter must produce the very bytes ``yaml.dump`` did, so an
existing reader of ``env.yaml`` cannot tell the two apart.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.output_writer import ENV_FILENAME, OutputWriter
from tolokaforge.core.redaction import REDACTED_PLACEHOLDER, SensitiveKeyRedaction
from tolokaforge.core.streaming_yaml import dump_mapping_streaming, libyaml_available

pytestmark = pytest.mark.unit


def _awkward_state() -> dict[Any, Any]:
    """Scalars whose plain text a YAML reader would misread, and every container edge."""
    records = [
        {
            "id": i,
            "name": f"n{i}",
            "balance": i * 1.5,
            "tiny": -1e-5,
            "huge_float": 1e17,
            "huge_int": 10**30,
            "flag": i % 2 == 0,
            "missing": None,
            "note": "line one\nline two\n" if i % 3 else "x\ny",
            "looks_like_bool": "yes",
            "looks_like_int": "123",
            "looks_like_float": "1.0",
            "looks_like_null": "null",
            "leading_space": " lead",
            "unicode": "Привет ✓",
            "empty": "",
            "colon": "a: b",
            "comment": "#x",
            "date_like": "2024-01-01",
            "tab": "a\tb",
            "long": "word " * 40,
        }
        for i in range(12)
    ]
    return {
        "agent": {"users": records, "empty_map": {}, "empty_list": []},
        "db": {7: "int key", 1.5: "float key", True: "bool key", None: "null key"},
        "filesystem": {"/env/fs/agent-visible/a.txt": "hello\n", "/env/b.bin": b"\x00\x01\xff"},
        "nested": [[1, [2, [3, {}]]], []],
        "special_floats": [float("inf"), float("-inf")],
        "agent_visible_dir": "/work",
        "represented_as_containers": {"tuple": (1, ("two", 3)), "set": {"only"}},
    }


def _yaml_dump(data: Any) -> str:
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _streamed(data: Any, *, use_libyaml: bool) -> str:
    out = io.StringIO()
    dump_mapping_streaming(data, out, use_libyaml=use_libyaml)
    return out.getvalue()


def test_python_emitter_writes_the_bytes_yaml_dump_wrote() -> None:
    data = _awkward_state()

    assert _streamed(data, use_libyaml=False) == _yaml_dump(data)


@pytest.mark.skipif(not libyaml_available(), reason="PyYAML built without libyaml")
def test_libyaml_emitter_reads_back_as_the_same_data() -> None:
    data = _awkward_state()
    data["emoji"] = "outside the BMP 😀"

    streamed = _streamed(data, use_libyaml=True)

    assert yaml.unsafe_load(streamed) == yaml.unsafe_load(_yaml_dump(data))


def test_safe_load_reads_a_json_shaped_state_back_unchanged() -> None:
    state = {"orders": [{"id": "O-1", "total": 12.5, "paid": False, "notes": None}]}

    assert yaml.safe_load(_streamed(state, use_libyaml=libyaml_available())) == state


def test_a_shared_container_is_written_twice_and_reads_back_equal() -> None:
    shared = {"rows": [1, 2, 3]}
    data = {"agent": shared, "db": shared}

    streamed = _streamed(data, use_libyaml=False)

    assert "&" not in streamed and "*" not in streamed
    assert yaml.safe_load(streamed) == {"agent": shared, "db": shared}


def test_a_cycle_is_refused_rather_than_aliased() -> None:
    data: dict[str, Any] = {"a": {}}
    data["a"]["self"] = data

    with pytest.raises(ValueError, match="contains itself"):
        _streamed(data, use_libyaml=False)


def test_write_env_state_streams_without_copying_under_no_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the default policy the mapping reaches the writer as given, not as a copy."""
    seen: list[Any] = []
    real = dump_mapping_streaming

    def spy(data: Any, stream: Any, **kwargs: Any) -> None:
        seen.append(data)
        real(data, stream, **kwargs)

    monkeypatch.setattr("tolokaforge.core.output_writer.dump_mapping_streaming", spy)
    env_state = _awkward_state()

    OutputWriter(tmp_path).write_env_state(env_state)

    assert seen == [env_state] and seen[0] is env_state
    written = (tmp_path / ENV_FILENAME).read_text()
    assert yaml.unsafe_load(written) == yaml.unsafe_load(_yaml_dump(env_state))


def test_write_env_state_still_redacts_under_a_redacting_policy(tmp_path: Path) -> None:
    env_state = {"db": {"users": [{"id": 1, "api_key": "sk-live"}]}, "agent_visible_dir": "/w"}

    OutputWriter(tmp_path, redaction=SensitiveKeyRedaction()).write_env_state(env_state)

    written = yaml.safe_load((tmp_path / ENV_FILENAME).read_text())
    assert written["db"]["users"][0]["api_key"] == REDACTED_PLACEHOLDER
    assert env_state["db"]["users"][0]["api_key"] == "sk-live"
