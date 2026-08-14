"""Minimal test for echo-hello task."""

from pathlib import Path


def test_hello_file_exists():
    assert Path("/tmp/hello.txt").exists()


def test_hello_file_content():
    assert Path("/tmp/hello.txt").read_text().strip() == "Hello, World!"
