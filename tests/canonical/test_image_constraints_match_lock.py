"""``tolokaforge/docker/image-constraints.txt`` pins exactly the registry packages of ``uv.lock``.

Image builds, the CI wheel venv and the test-lane wheel venvs install under this file, so a
stale export would let them install versions the lock never chose. The pre-commit hook
``image-constraints`` regenerates it whenever ``uv.lock`` changes.
"""

from __future__ import annotations

import tomllib

import pytest
import yaml

from tests.utils.image_constraints import (
    IMAGE_CONSTRAINTS_PATH,
    REPO_ROOT,
    linux_sdist_only_packages,
    lock_registry_pins,
    parse_constraint_lines,
)

pytestmark = pytest.mark.canonical

_HOOK_ID = "image-constraints"
_REGENERATE = f"uv run pre-commit run {_HOOK_ID} --files uv.lock"


def _constraint_pins() -> frozenset[tuple[str, str]]:
    return frozenset(
        (pin.name, pin.version)
        for pin in parse_constraint_lines(IMAGE_CONSTRAINTS_PATH.read_text())
    )


def test_constraints_pin_exactly_the_lock_registry_packages() -> None:
    constraints = _constraint_pins()
    lock = lock_registry_pins()
    missing = sorted(lock - constraints)
    extra = sorted(constraints - lock)
    assert not missing and not extra, (
        f"{IMAGE_CONSTRAINTS_PATH.relative_to(REPO_ROOT)} drifted from uv.lock; "
        f"missing pins: {missing}; extra pins: {extra}. Regenerate with `{_REGENERATE}`."
    )


@pytest.mark.parametrize(
    "line",
    [
        "litellm>=1.93.0",
        "-e ./tolokaforge_models",
        "--index-url https://download.pytorch.org/whl/cpu",
        "tolokaforge-models @ file:///src/tolokaforge_models",
        "litellm==1.93.0 \\",
    ],
)
def test_a_line_that_is_not_an_exact_pin_is_refused(line: str) -> None:
    with pytest.raises(ValueError, match="not a `name==version"):
        parse_constraint_lines(f"addict==2.4.0\n{line}\n")


def test_litellm_pin_is_the_locked_version() -> None:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    (locked,) = {package["version"] for package in lock["package"] if package["name"] == "litellm"}
    pinned = {
        pin.version
        for pin in parse_constraint_lines(IMAGE_CONSTRAINTS_PATH.read_text())
        if pin.name == "litellm"
    }
    assert pinned == {locked}


def test_linux_sdist_only_set_follows_the_linux_applicable_pin() -> None:
    sdist_only = linux_sdist_only_packages()
    assert "sly" in sdist_only, "sly 0.4 ships no wheel at all"
    assert "litellm" not in sdist_only, "litellm ships a py3-none-any wheel"
    torch_reason = (
        "the darwin-only torch pin has no Linux wheel; the Linux pin (+cpu) has manylinux wheels"
    )
    assert "torch" not in sdist_only, torch_reason


def test_hook_regenerates_the_file_from_uv_lock() -> None:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    (hook,) = [hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == _HOOK_ID]
    assert hook["files"] == r"^uv\.lock$"
    assert hook["pass_filenames"] is False
    assert hook["entry"].endswith(f"-o {IMAGE_CONSTRAINTS_PATH.relative_to(REPO_ROOT)}")
