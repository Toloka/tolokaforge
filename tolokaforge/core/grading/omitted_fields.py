"""Leaving an optional field out of a model's dump while it is absent.

A field added to a model that already crosses the wire or lands in a recorded file —
a trial spec, a grading config, a grade — would otherwise dump as ``null`` on every
instance that never sets it: a key an older ``extra="forbid"`` reader refuses, and a
byte change to every snapshot and bundle that predates it. A model names such fields in
an ``omitted_when_absent`` class attribute and wraps its serializer around
:func:`leave_out_absent_fields`::

    omitted_when_absent: ClassVar[frozenset[str]] = frozenset({"comparison_view"})

    @model_serializer(mode="wrap")
    @schema_from_the_fields
    def _leave_out_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return leave_out_absent_fields(self, handler)

:func:`schema_from_the_fields` is what keeps the model's serialization JSON schema: pydantic
derives a wrap serializer's schema from its return annotation, so a ``dict[str, Any]`` one
erases the schema of the model and of every model holding it, while a serializer with no
return annotation keeps the schema its fields give. The annotation stays for the type
checker; only the runtime one is dropped.

The wire census of ``tests/canonical/test_grading_wire_lock.py`` reads the same class
attribute, so a key a dump leaves out is censused as emitted only by a pack declaring it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, SerializerFunctionWrapHandler

__all__ = ["leave_out_absent_fields", "schema_from_the_fields"]

_Serializer = TypeVar("_Serializer", bound=Callable[..., Any])


def schema_from_the_fields(serializer: _Serializer) -> _Serializer:
    """Drop a model serializer's runtime return annotation, so the model keeps its schema."""
    serializer.__annotations__.pop("return", None)
    return serializer


def leave_out_absent_fields(
    model: BaseModel, handler: SerializerFunctionWrapHandler
) -> dict[str, Any]:
    """``model``'s dump without the ``omitted_when_absent`` fields that are ``None``."""
    dumped: dict[str, Any] = handler(model)
    for name in getattr(type(model), "omitted_when_absent", frozenset()):
        if getattr(model, name) is None:
            dumped.pop(name, None)
    return dumped
