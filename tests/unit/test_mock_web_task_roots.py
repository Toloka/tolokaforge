"""Unit tests for mock-web multi-root task resolution."""

from pathlib import Path

import pytest

from tolokaforge.env.mock_web_service import app as mock_app

pytestmark = pytest.mark.unit


def _write_json(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_find_static_file_prefers_first_root_and_shared_assets(tmp_path: Path, monkeypatch):
    root_a = tmp_path / "pack_a" / "tasks"
    root_b = tmp_path / "pack_b" / "tasks"

    a_index = root_a / "browser" / "task_001" / "www" / "site" / "index.html"
    b_index = root_b / "browser" / "task_001" / "www" / "site" / "index.html"
    _write_json(a_index, "<html>A</html>")
    _write_json(b_index, "<html>B</html>")

    shared_asset = root_a / "browser" / "_assets" / "bundle.js"
    _write_json(shared_asset, "console.log('shared');")

    monkeypatch.setattr(mock_app, "TASK_ROOTS", [root_a, root_b])

    resolved_index = mock_app.find_static_file("/task/browser/task_001/index.html")
    assert resolved_index == a_index

    resolved_shared = mock_app.find_static_file("/task/browser/task_001/bundle.js")
    assert resolved_shared == shared_asset


def test_load_dataset_searches_all_roots(tmp_path: Path, monkeypatch):
    root_a = tmp_path / "pack_a" / "tasks"
    root_b = tmp_path / "pack_b" / "tasks"

    dataset_dir = root_b / "mobile" / "_data" / "v1"
    _write_json(dataset_dir / "places.json", "[]")
    _write_json(dataset_dir / "menus.json", "[]")

    monkeypatch.setattr(mock_app, "TASK_ROOTS", [root_a, root_b])
    mock_app._load_dataset.cache_clear()

    dataset = mock_app._load_dataset("v1")
    assert dataset["dataset"] == "v1"
    assert dataset["places"] == []
    assert dataset["menus"] == []


def test_load_dataset_missing_includes_root_diagnostics(tmp_path: Path, monkeypatch):
    root_a = tmp_path / "pack_a" / "tasks"
    root_b = tmp_path / "pack_b" / "tasks"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)

    monkeypatch.setattr(mock_app, "TASK_ROOTS", [root_a, root_b])
    mock_app._load_dataset.cache_clear()

    try:
        mock_app._load_dataset("v_missing")
    except FileNotFoundError as exc:
        msg = str(exc)
        assert "Dataset not found: v_missing" in msg
        assert str(root_a) in msg
        assert str(root_b) in msg
    else:
        raise AssertionError("Expected FileNotFoundError for missing dataset")


def test_find_static_file_supports_www_direct_relative_paths(tmp_path: Path, monkeypatch):
    root = tmp_path / "pack" / "tasks"
    index = root / "deep_research" / "task_100" / "www" / "research-hub" / "index.html"
    nested = root / "deep_research" / "task_100" / "www" / "source-1" / "index.html"
    _write_json(index, "<html>hub</html>")
    _write_json(nested, "<html>source</html>")

    monkeypatch.setattr(mock_app, "TASK_ROOTS", [root])

    resolved_hub = mock_app.find_static_file("/task/deep_research/task_100/index.html")
    resolved_source = mock_app.find_static_file("/task/deep_research/task_100/source-1/index.html")

    assert resolved_hub == index
    assert resolved_source == nested
