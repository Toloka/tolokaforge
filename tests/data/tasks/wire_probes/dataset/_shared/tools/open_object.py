"""Neutral wire-probe tools: an ARRAY nested inside an OPEN (free-form) object.

Deliberately NEUTRAL and STUBBED (no domain flavour), like the other records-sandbox tools.
They exercise one structural wire-shape class that the pack was otherwise missing: a nested
array whose type is HIDDEN because it lives inside an open object.

The pattern: a parameter typed ``dict[str, Any]`` advertises as ``{"type": "object",
"additionalProperties": true}`` - so the wire schema the model sees does NOT declare the inner
``tags`` field as an array (the type is hidden), yet the tool strictly requires ``tags`` to be a
list. A provider that mangles arrays (e.g. an XML->JSON conversion emitting ``{"item": [...]}``,
a JSON-encoded string, or ``""``) is REJECTED with a message that NAMES the site
(``<param>.tags``). This is the signal the existing tools could not emit: ``attach_metadata``
(``dict[str, Any]`` with ``Any`` inner) accepts any shape and never rejects, and ``update_record``
(``tags: list[str]``, a DECLARED top-level array) is the easy schema-visible case.

Non-scoring stubs: they validate the ``tags`` shape then echo. ``data: dict`` is the state arg.
No ``from __future__ import annotations`` (FastMCP resolves the live signature types).
"""

from typing import Annotated, Any

from pydantic import Field

from tolokaforge.core.tools_interface import DomainToolRegistry


def _require_tag_list(param: str, container: Any) -> None:
    """Reject unless ``container['tags']`` (if present) is a real ``list[str]``.

    The message mirrors a strict server validator ("Input should be a valid list") and NAMES the
    path (``<param>.tags``) so the observe census records the exact nested array site a recovery
    policy must target - the signal a hidden-type array otherwise cannot emit. If ``container``
    is not even an object (e.g. an over-broad fix listified it), that is reported too.
    """
    if not isinstance(container, dict):
        raise ValueError(f"{param}: Input should be a valid object, got {type(container).__name__}")
    if "tags" not in container:
        return
    tags = container["tags"]
    if not (isinstance(tags, list) and all(isinstance(x, str) for x in tags)):
        raise ValueError(
            f"{param}.tags: Input should be a valid list of strings, got {type(tags).__name__}"
        )


def register(registry: DomainToolRegistry) -> None:
    @registry.tool(
        "Apply an update to a record. `patch` is an OPEN object (any string keys); when it "
        "carries a `tags` field, that field must be a JSON array of strings."
    )
    def apply_record_update(
        data: dict,
        record_id: Annotated[str, Field(description="Record id.", examples=["REC-1001"])],
        patch: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "Open patch object with free-form keys. A `tags` key, if present, is a "
                    "list of strings."
                ),
                examples=[{"status": "active", "tags": ["alpha", "beta"]}],
            ),
        ],
    ) -> dict:
        _require_tag_list("patch", patch)
        return {"ok": True, "record_id": record_id, "patch_keys": sorted(patch.keys())}

    @registry.tool(
        "Submit a new record. `record` is an OPEN object (any string keys); when it carries a "
        "`tags` field, that field must be a JSON array of strings."
    )
    def submit_record(
        data: dict,
        record: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "Open record object with free-form keys. A `tags` key, if present, is a "
                    "list of strings."
                ),
                examples=[{"name": "example", "tags": ["alpha", "beta"]}],
            ),
        ],
    ) -> dict:
        _require_tag_list("record", record)
        return {"ok": True, "record_keys": sorted(record.keys())}
