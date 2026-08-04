"""Finalize gate: a new engine policy class must ship a unit test.

Every policy class the resolve agent writes has to be registered in a
``_POLICY_REGISTRIES`` slot (``tolokaforge/core/llm/presets.py``) to be referenceable
from a preset, so the staged diff of that file is a complete, deterministic index of
what the integration added. This gate reads it and requires two things per new class:

  1. A unit test under ``tests/unit/llm/`` mentions the class by name. ``docs/ADD_NEW_MODEL.md``
     step 5 already mandates this ("add a unit-test fixture ... so the codec round-trip is
     unit-testable without burning provider spend"), but nothing enforced it: the fix-loop's
     success signal is the live reprobe, so a missing unit test was invisible.
  2. The class is actually referenced from the overlay. An unreferenced new class is dead
     public API - the agent solved the probe some other way and left the class behind.

Why this matters beyond hygiene: a unit test is the cheapest place where a *reviewer* sees
the class's real input/output shape. Both defects this gate targets shipped together in
PR #846 (a new reasoning codec with no unit test, whose behaviour a shipped codec already
covered - see ``PAYLOAD_ONLY_CAPABILITIES`` in :mod:`automation.cert`).

``run`` returns an exit code (1 on any violation, 0 otherwise); the pure helpers below are
unit-tested without touching git.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

REGISTRY_FILE = "tolokaforge/core/llm/presets.py"
DEFAULT_TESTS_DIR = "tests/unit/llm"

# ``"json_coerce": JsonCoerceResponse,`` inside a _POLICY_REGISTRIES slot.
_REGISTRY_BINDING = re.compile(r'^\+\s*"[A-Za-z0-9_]+"\s*:\s*([A-Z][A-Za-z0-9_]*)\s*,')
_DIFF_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")


def added_registry_classes(diff_text: str) -> list[str]:
    """Class names newly bound into a ``_POLICY_REGISTRIES`` slot by this diff.

    Scoped to added lines inside the registry file's hunks, so a class merely *defined*
    elsewhere in the diff (or a registry line that only moved) is not reported. Order is
    deduplicated-stable so the failure message reads deterministically.
    """
    found: list[str] = []
    in_registry_file = False
    for line in diff_text.splitlines():
        header = _DIFF_FILE_HEADER.match(line)
        if header:
            in_registry_file = header.group(1) == REGISTRY_FILE
            continue
        if not in_registry_file:
            continue
        match = _REGISTRY_BINDING.match(line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def unreferenced(classes: list[str], overlay_text: str, diff_text: str) -> list[str]:
    """New classes named by neither the overlay nor a preset entry in the diff.

    An overlay references a class through its registry KEY, not its name, so match on
    either: the class name appearing in the diff's preset YAML hunks, or the registry key
    it was bound to appearing in the overlay. Keeping this permissive is deliberate - the
    gate is here to catch a class nobody wired up at all, not to police style.
    """
    keys_by_class = _registry_keys(diff_text)
    missing = []
    for name in classes:
        key = keys_by_class.get(name, "")
        referenced = name in overlay_text or (key and key in overlay_text)
        if not referenced:
            missing.append(name)
    return missing


def _registry_keys(diff_text: str) -> dict[str, str]:
    """``{ClassName: registry_key}`` for bindings added in the registry file."""
    keys: dict[str, str] = {}
    in_registry_file = False
    pattern = re.compile(r'^\+\s*"([A-Za-z0-9_]+)"\s*:\s*([A-Z][A-Za-z0-9_]*)\s*,')
    for line in diff_text.splitlines():
        header = _DIFF_FILE_HEADER.match(line)
        if header:
            in_registry_file = header.group(1) == REGISTRY_FILE
            continue
        if in_registry_file:
            match = pattern.match(line)
            if match:
                keys.setdefault(match.group(2), match.group(1))
    return keys


def untested(classes: list[str], test_blob: str) -> list[str]:
    """New classes no unit-test source mentions by name."""
    return [name for name in classes if name not in test_blob]


def read_tests(tests_dir: str = DEFAULT_TESTS_DIR) -> str:
    """Concatenate every unit-test source under ``tests_dir`` (missing dir -> empty)."""
    root = pathlib.Path(tests_dir)
    if not root.is_dir():
        return ""
    return "\n".join(path.read_text(errors="replace") for path in sorted(root.rglob("test_*.py")))


def _staged_diff() -> str:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--", REGISTRY_FILE],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def run(overlay_path: str | None = None, tests_dir: str = DEFAULT_TESTS_DIR) -> int:
    """Gate the STAGED diff. Returns 1 on any violation (finalize routes that to
    needs-human), 0 otherwise. No new class -> nothing to check -> 0."""
    diff_text = _staged_diff()
    classes = added_registry_classes(diff_text)
    if not classes:
        print("classgate: OK (no new policy class registered)")
        return 0

    overlay_text = ""
    if overlay_path and pathlib.Path(overlay_path).is_file():
        overlay_text = pathlib.Path(overlay_path).read_text(errors="replace")

    violations = [
        f"UNTESTED-CLASS: `{name}` is registered in {REGISTRY_FILE} but no unit test under "
        f"{tests_dir}/ mentions it. docs/ADD_NEW_MODEL.md step 5 requires a unit test (with a "
        "captured real-response fixture) for a new policy class - the live reprobe does not "
        "substitute for one."
        for name in untested(classes, read_tests(tests_dir))
    ]
    violations += [
        f"UNREFERENCED-CLASS: `{name}` is registered in {REGISTRY_FILE} but neither the "
        "overlay nor a preset entry references it - dead public API. Wire it up or drop it."
        for name in unreferenced(classes, overlay_text, diff_text)
    ]

    for violation in violations:
        print(f"::error::classgate: {violation}")
    if violations:
        print(f"classgate: FAIL ({len(violations)} violation(s) over {len(classes)} new class(es))")
        return 1
    print(f"classgate: OK ({len(classes)} new class(es) tested and referenced)")
    return 0
