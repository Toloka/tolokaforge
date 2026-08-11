"""Finalize gate: a new engine policy class must ship a unit test.

Every policy class the resolve agent writes has to be registered in a
``_POLICY_REGISTRIES`` slot (``tolokaforge/core/llm/presets.py``) to be referenceable
from a preset, so the registry is a complete, deterministic index of what the
integration added. This gate compares the STAGED registry against HEAD and requires
two things per newly added binding:

  1. A unit test under ``tests/unit/llm/`` - read from the INDEX, i.e. the tree the
     finalize commit will actually ship - mentions the bound class by name.
     ``docs/ADD_NEW_MODEL.md`` step 5 already mandates this ("add a unit-test fixture
     ... so the codec round-trip is unit-testable without burning provider spend"),
     but nothing enforced it: the fix-loop's success signal is the live reprobe, so a
     missing unit test was invisible. The mention is name-level (word-boundary), not
     an executed test - the reviewer still checks the test is real.
  2. The class is actually referenced - by registry key in a VALUE position of the
     overlay or of the staged ``model_presets.yaml``, or by class name in either. An
     unreferenced new class is dead public API: the agent solved the probe some other
     way and left the class behind.

Why this matters beyond hygiene: a unit test is the cheapest place where a *reviewer*
sees the class's real input/output shape. Both defects this gate targets shipped
together in PR #846 (a new reasoning codec with no unit test, whose behaviour a
shipped codec already covered - see ``PAYLOAD_ONLY_CAPABILITIES`` in
:mod:`automation.cert`).

The registry comparison is an AST set-diff, not a diff-text regex: quoting style,
trailing commas, wrapped lines, dotted values, instance bindings and factory names
cannot hide a binding, and a moved or reordered line never reads as new. Registration
shapes are collected over the whole tree (``if``/``try`` nesting included):
dict-literal (re)assignment, ``_SLOT["key"] = Value``, ``_SLOT |= {...}``,
``_SLOT.update({...})`` / ``_SLOT.update(k=V)``, and ``_SLOT.setdefault("k", V)``.
This is a guard, so it FAILS LOUD instead of skipping: any git error, an unparsable
staged registry, a registry with no discoverable ``_POLICY_REGISTRIES`` slot, and any
slot-registry mutation the gate cannot model (a non-dict reassignment, another
AugAssign shape, ``update(**splat)``, an unrecognized method call) all exit 1
(needs-human).

``run`` returns an exit code (1 on any violation or read failure, 0 otherwise); the
pure helpers below are unit-tested without git, and ``run`` itself against throwaway
git repos.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
from typing import NamedTuple

REGISTRY_FILE = "tolokaforge/core/llm/presets.py"
PRESETS_FILE = "tolokaforge/core/data/model_presets.yaml"
DEFAULT_TESTS_DIR = "tests/unit/llm"

# The repo root anchored to THIS file (tools/automation/src/automation/classgate.py),
# like cert._load_cert - never the caller's CWD, which silently empties every git
# answer from a subdirectory and turns the gate into a no-op.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


class Binding(NamedTuple):
    """One registry entry: which slot registry, under which key, bound to what."""

    registry: str
    key: str
    value: str


def _expr_repr(node: ast.expr) -> str:
    """A stable identifier rendering for a binding value.

    ``Name`` -> ``Cls``; ``Attribute`` -> ``pkg.Cls``; ``Call`` -> ``Cls()`` (an
    instance or factory binding still names what was bound); anything else falls back
    to ``ast.dump`` so an exotic shape still produces a DISTINCT, diffable binding
    (detected as new -> gated) rather than vanishing.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expr_repr(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_expr_repr(node.func)}()"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ast.dump(node)


def _key_repr(node: ast.expr) -> str:
    """Registry keys are string constants; anything else renders via ``_expr_repr``
    so a computed key still surfaces as a (necessarily unreferenced) new binding."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return _expr_repr(node)


def _assign_target(node: ast.stmt) -> str | None:
    """The single ``Name`` target of an ``Assign``/``AnnAssign``, else ``None``."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        return target.id if isinstance(target, ast.Name) else None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _dict_bindings(registry: str, node: ast.Dict) -> set[Binding]:
    return {
        Binding(registry, _key_repr(key), _expr_repr(value))
        for key, value in zip(node.keys, node.values)
        if key is not None  # a ``**spread`` entry has no literal key to gate on
    }


# Slot-var method calls that only READ (the real presets.py resolves policies via
# ``_X.get(...)`` and passes the dicts as function arguments). Any OTHER method call
# on a slot registry is an un-modelable mutation and fails the gate loud.
_READ_ONLY_METHODS = frozenset({"get", "items", "keys", "values", "copy"})


def registry_bindings(source: str) -> set[Binding]:
    """Every registry binding in a ``presets.py`` source, as a set.

    Slot registries are discovered from the ``_POLICY_REGISTRIES`` index itself (its
    dict values name the per-slot dicts, or carry inline dicts), so a brand-new slot
    the agent wires in is covered too. Mutations are collected over the WHOLE tree
    (``if``/``try`` nesting cannot hide a registration) in every modelable shape:
    dict-literal (re)assignment, ``_SLOT["k"] = V``, ``_SLOT |= {...}``,
    ``_SLOT.update({...})`` / ``_SLOT.update(k=V)``, ``_SLOT.setdefault("k", V)``.
    Anything else that could mutate a slot registry - a non-dict reassignment, another
    AugAssign shape, ``update(**splat)``, an unrecognized method call - RAISES, as do an
    unparsable source and a registry with no discoverable slot: a guard that cannot
    model what it sees must fail loud, never skip.
    """
    tree = ast.parse(source)
    slot_vars: set[str] = set()
    bindings: set[Binding] = set()
    for node in tree.body:
        if _assign_target(node) != "_POLICY_REGISTRIES":
            continue
        index = node.value
        if not isinstance(index, ast.Dict):
            raise ValueError("_POLICY_REGISTRIES is not a dict literal - the gate cannot read it")
        for key, value in zip(index.keys, index.values):
            if isinstance(value, ast.Name):
                slot_vars.add(value.id)
            elif isinstance(value, ast.Dict):
                bindings |= _dict_bindings(_key_repr(key) if key else "?", value)
    if not slot_vars and not bindings:
        raise ValueError(
            f"no _POLICY_REGISTRIES slots found in {REGISTRY_FILE} - the gate cannot "
            "see the registry"
        )
    for node in ast.walk(tree):
        target = _assign_target(node) if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if target in slot_vars:
            value = getattr(node, "value", None)
            if isinstance(value, ast.Dict):
                bindings |= _dict_bindings(target, value)
            elif value is not None:
                raise ValueError(
                    f"unmodelable reassignment of registry {target} (not a dict literal)"
                )
            continue
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(sub := node.targets[0], ast.Subscript)
            and isinstance(sub.value, ast.Name)
            and sub.value.id in slot_vars
        ):
            bindings.add(Binding(sub.value.id, _key_repr(sub.slice), _expr_repr(node.value)))
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id in slot_vars:
                if isinstance(node.op, ast.BitOr) and isinstance(node.value, ast.Dict):
                    bindings |= _dict_bindings(node.target.id, node.value)
                else:
                    raise ValueError(
                        f"unmodelable augmented assignment to registry {node.target.id}"
                    )
            elif (
                isinstance(node.target, ast.Subscript)
                and isinstance(node.target.value, ast.Name)
                and node.target.value.id in slot_vars
            ):
                raise ValueError(
                    f"unmodelable augmented item assignment on registry {node.target.value.id}"
                )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (slot := node.func.value.id) in slot_vars
        ):
            method = node.func.attr
            if method == "update":
                for keyword in node.keywords:
                    if keyword.arg is None:  # **splat hides its keys from the gate
                        raise ValueError(f"unmodelable update(**...) on registry {slot}")
                    bindings.add(Binding(slot, keyword.arg, _expr_repr(keyword.value)))
                if node.args:
                    if len(node.args) == 1 and isinstance(node.args[0], ast.Dict):
                        bindings |= _dict_bindings(slot, node.args[0])
                    else:
                        raise ValueError(f"unmodelable update(...) argument on registry {slot}")
            elif method == "setdefault":
                if len(node.args) == 2:
                    bindings.add(Binding(slot, _key_repr(node.args[0]), _expr_repr(node.args[1])))
                else:
                    raise ValueError(f"unmodelable setdefault(...) on registry {slot}")
            elif method not in _READ_ONLY_METHODS:
                raise ValueError(f"unmodelable method call .{method}() on registry {slot}")
    return bindings


def added_bindings(staged_source: str, head_source: str) -> list[Binding]:
    """Bindings present in the staged registry but not in HEAD's, sorted.

    A set diff, so a binding that merely moved (the agent alphabetized a dict while
    inserting its entry) is not reported - only genuinely new (registry, key, value)
    triples are.
    """
    return sorted(registry_bindings(staged_source) - registry_bindings(head_source))


def bound_name(value_repr: str) -> str:
    """``codecs.BrandNewCodec()`` -> ``BrandNewCodec``: the identifier a test must name."""
    return value_repr.removesuffix("()").rsplit(".", 1)[-1]


def untested(names: list[str], test_blob: str) -> list[str]:
    """Names no unit-test source mentions as a whole word.

    Word-boundary, not substring: an existing mention of ``MinimaxM3TagRecoveryResponse``
    must not vouch for a new ``TagRecoveryResponse``.
    """
    return [name for name in names if not re.search(rf"\b{re.escape(name)}\b", test_blob)]


def _key_referenced(key: str, text: str) -> bool:
    """A registry key counts as referenced only in a YAML VALUE position
    (``slot: key`` / ``slot: "key"``), not as a raw substring - a short family key
    like ``qwen`` must not be satisfied by a ``match:`` glob that merely contains it."""
    return re.search(rf""":\s*['"]?{re.escape(key)}\b""", text) is not None


def unreferenced(bindings: list[Binding], overlay_text: str, presets_text: str) -> list[str]:
    """Bound names that neither the overlay nor the staged preset file references.

    A reference is any of that name's registry keys in a value position, or the name
    itself as a whole word, in either source. Keeping this permissive is deliberate -
    the gate catches a class nobody wired up at all, not style.
    """
    keys_by_name: dict[str, set[str]] = {}
    for binding in bindings:
        keys_by_name.setdefault(bound_name(binding.value), set()).add(binding.key)
    combined = (overlay_text, presets_text)
    return sorted(
        name
        for name, keys in keys_by_name.items()
        if not any(_key_referenced(key, text) for key in keys for text in combined)
        and not any(re.search(rf"\b{re.escape(name)}\b", text) for text in combined)
    )


def _git(args: list[str], root: pathlib.Path) -> str:
    """Run git anchored at ``root``; any failure raises with git's own stderr.

    A guard must fail loud: swallowing a nonzero exit here is how "not a git repo"
    or a wrong CWD used to read as "no new policy class registered".
    """
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _git_optional(args: list[str], root: pathlib.Path) -> str:
    """Like ``_git`` but a path absent from the requested tree reads as empty - used
    only for reference SOURCES (a missing preset file just means no reference can
    come from it), never for the registry the gate exists to judge."""
    try:
        return _git(args, root)
    except RuntimeError as exc:
        message = str(exc)
        if "does not exist" in message or "but not in" in message:
            return ""
        raise


def read_index_tests(root: pathlib.Path, tests_dir: str = DEFAULT_TESTS_DIR) -> str:
    """Concatenate every ``test_*.py`` under ``tests_dir`` as staged in the INDEX.

    The index is the tree the finalize commit ships. A worktree-only test file (never
    ``git add``-ed) deliberately does not count: it would satisfy the gate and then
    vanish from the commit - the exact false assurance this gate exists to prevent.
    """
    listed = _git(["ls-files", "--cached", "--", tests_dir], root)
    sources = []
    for path in sorted(line for line in listed.splitlines() if line):
        name = pathlib.PurePosixPath(path).name
        if name.startswith("test_") and name.endswith(".py"):
            sources.append(_git(["show", f":{path}"], root))
    return "\n".join(sources)


def run(
    overlay_path: str | None = None,
    tests_dir: str = DEFAULT_TESTS_DIR,
    root: str | None = None,
) -> int:
    """Gate the STAGED registry against HEAD. Returns 1 on any violation or read
    failure (finalize routes both to needs-human), 0 otherwise. No new binding ->
    nothing to check -> 0."""
    repo = pathlib.Path(root).resolve() if root else REPO_ROOT
    try:
        staged = _git(["show", f":{REGISTRY_FILE}"], repo)
        head = _git(["show", f"HEAD:{REGISTRY_FILE}"], repo)
        added = added_bindings(staged, head)
        if not added:
            print("classgate: OK (no new policy class registered)")
            return 0
        test_blob = read_index_tests(repo, tests_dir)
        presets_text = _git_optional(["show", f":{PRESETS_FILE}"], repo)
    except Exception as exc:  # fail loud - a guard must never silently pass
        print(f"::error::classgate could not read the staged tree: {exc}")
        return 1

    overlay_text = ""
    if overlay_path and pathlib.Path(overlay_path).is_file():
        overlay_text = pathlib.Path(overlay_path).read_text(errors="replace")

    names = sorted({bound_name(binding.value) for binding in added})
    violations = [
        f"UNTESTED-CLASS: `{name}` is registered in {REGISTRY_FILE} but no STAGED unit test "
        f"under {tests_dir}/ mentions it. docs/ADD_NEW_MODEL.md step 5 requires a unit test "
        "(prefer a captured real-response fixture) for a new policy class - the live reprobe "
        "does not substitute for one, and a test left unstaged would not ship."
        for name in untested(names, test_blob)
    ]
    violations += [
        f"UNREFERENCED-CLASS: `{name}` is registered in {REGISTRY_FILE} but neither the "
        f"overlay nor the staged {PRESETS_FILE} references it (by key in a value position, "
        "or by name) - dead public API. Wire it up or drop it."
        for name in unreferenced(added, overlay_text, presets_text)
    ]

    for violation in violations:
        print(f"::error::classgate: {violation}")
    if violations:
        print(f"classgate: FAIL ({len(violations)} violation(s) over {len(names)} new class(es))")
        return 1
    print(f"classgate: OK ({len(names)} new class(es) tested and referenced)")
    return 0
