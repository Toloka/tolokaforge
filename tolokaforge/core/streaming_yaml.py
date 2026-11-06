"""Write a large mapping as YAML without building PyYAML's node graph.

``yaml.dump`` represents the whole document as a graph of ``Node`` objects
before the first byte is emitted, one Python object per scalar, mapping and
sequence, and keeps the graph alive until the document is written. For the
final environment state of a task whose world is data, that graph is several
times the size of the state itself, and the pure-Python emitter then walks it a
character at a time.

:func:`dump_mapping_streaming` walks the data instead and hands the emitter one
event at a time, so memory stays bounded by the nesting depth rather than by
the size of the document. Exact ``str``, ``int``, ``float``, ``bool`` and
``None`` scalars are rendered the way ``SafeRepresenter`` renders them, with a
string that contains a newline in literal-block style — the representer
:mod:`tolokaforge.core.output_writer` registers for every bundle file. Every
other value whose type is not exactly ``dict`` or ``list`` goes through
``yaml.Dumper``'s own representers. Implicit resolution is the dumper's. With the
pure-Python emitter the output is byte-identical to ``yaml.dump(data,
default_flow_style=False, allow_unicode=True, sort_keys=False)`` (with that
representer registered) for acyclic data whose containers are not shared; with
libyaml's emitter (used when PyYAML was built with it) the bytes can differ in
layout and escaping — libyaml escapes characters outside the Basic Multilingual
Plane — never in what ``yaml.safe_load`` reads back.

Two differences from ``yaml.dump``, both deliberate:

* A ``dict`` or ``list`` reached twice is written twice, not as an anchor and an
  alias: finding shared containers needs a pass over the whole document first,
  which is the cost this module exists to avoid. ``yaml.safe_load`` reads both
  forms as equal data.
* A cycle through ``dict`` / ``list`` containers raises ``ValueError`` instead of
  being written as a recursive alias. The final environment state is parsed off
  the wire and is acyclic by construction.
"""

from __future__ import annotations

from typing import IO, Any

import yaml
from yaml.events import (
    DocumentEndEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
    StreamEndEvent,
    StreamStartEvent,
)
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.representer import Representer
from yaml.resolver import Resolver
from yaml.serializer import Serializer

__all__ = ["dump_mapping_streaming", "libyaml_available"]


def libyaml_available() -> bool:
    """Whether PyYAML was built with libyaml, so the C emitter can be used."""
    return bool(getattr(yaml, "__with_libyaml__", False))


def _dumper_class(use_libyaml: bool) -> type:
    """A dumper whose representers are ``yaml.Dumper``'s and whose emitter is C or Python.

    The representer tables are shared with ``yaml.Dumper`` rather than copied, so a
    representer registered on ``yaml.Dumper`` (the engine registers one for
    multiline strings) applies here as it does to ``yaml.dump``.
    """
    if use_libyaml:
        from yaml._yaml import CEmitter  # type: ignore[import-not-found]

        emitter: type = CEmitter
    else:
        from yaml.emitter import Emitter

        emitter = Emitter

    class _StreamingDumper(emitter, Serializer, Representer, Resolver):  # type: ignore[misc, valid-type]
        def __init__(self, stream: IO[str]) -> None:
            emitter.__init__(
                self,
                stream,
                canonical=None,
                indent=None,
                width=None,
                allow_unicode=True,
                line_break=None,
            )
            Serializer.__init__(self)
            Representer.__init__(
                self, default_style=None, default_flow_style=False, sort_keys=False
            )
            Resolver.__init__(self)

    _StreamingDumper.yaml_representers = yaml.Dumper.yaml_representers
    _StreamingDumper.yaml_multi_representers = yaml.Dumper.yaml_multi_representers
    return _StreamingDumper


def dump_mapping_streaming(
    data: dict[Any, Any], stream: IO[str], *, use_libyaml: bool | None = None
) -> None:
    """Write *data* to the text *stream* as one block-style YAML document.

    Equivalent to ``yaml.dump(data, stream, default_flow_style=False,
    allow_unicode=True, sort_keys=False)`` with the differences the module
    docstring names. ``use_libyaml`` picks the emitter; ``None`` uses libyaml when
    it is available.

    Raises:
        ValueError: *data* reaches a ``dict`` or ``list`` that contains itself.
    """
    if use_libyaml is None:
        use_libyaml = libyaml_available()
    dumper = _dumper_class(use_libyaml)(stream)
    try:
        dumper.emit(StreamStartEvent(encoding=None))
        dumper.emit(DocumentStartEvent(explicit=False, version=None, tags=None))
        _emit_value(dumper, data, set())
        dumper.emit(DocumentEndEvent(explicit=False))
        dumper.emit(StreamEndEvent())
    finally:
        dumper.dispose()


_MAPPING_TAG = Resolver.DEFAULT_MAPPING_TAG
_SEQUENCE_TAG = Resolver.DEFAULT_SEQUENCE_TAG
_STR_TAG = Resolver.DEFAULT_SCALAR_TAG
_FLOAT_TAG = "tag:yaml.org,2002:float"
_PLAIN_ONLY = (True, False)

# The exact-type scalars whose plain text always resolves back to their own tag,
# rendered the way ``SafeRepresenter`` renders them. ``bool`` is listed on its own:
# the lookup is by exact type, so ``True`` never reaches the ``int`` entry.
_FIXED_SCALARS: dict[type, tuple[str, Any]] = {
    bool: ("tag:yaml.org,2002:bool", lambda value: "true" if value else "false"),
    int: ("tag:yaml.org,2002:int", str),
    type(None): ("tag:yaml.org,2002:null", lambda _value: "null"),
}


def _float_text(value: float) -> str:
    """``SafeRepresenter.represent_float``'s text for *value*."""
    if value != value:
        return ".nan"
    if value == Representer.inf_value:
        return ".inf"
    if value == -Representer.inf_value:
        return "-.inf"
    text = repr(value).lower()
    if "." not in text and "e" in text:
        text = text.replace("e", ".0e", 1)
    return text


def _emit_value(dumper: Any, value: Any, open_containers: set[int]) -> None:
    kind = type(value)
    if kind is dict or kind is list:
        marker = id(value)
        if marker in open_containers:
            raise ValueError(
                "the mapping to write contains itself; a streaming YAML dump writes "
                "acyclic data only"
            )
        open_containers.add(marker)
        emit = dumper.emit
        if kind is dict:
            emit(MappingStartEvent(None, _MAPPING_TAG, True, flow_style=False))
            for key, item in value.items():
                _emit_value(dumper, key, open_containers)
                _emit_value(dumper, item, open_containers)
            emit(MappingEndEvent())
        else:
            emit(SequenceStartEvent(None, _SEQUENCE_TAG, True, flow_style=False))
            for item in value:
                _emit_value(dumper, item, open_containers)
            emit(SequenceEndEvent())
        open_containers.discard(marker)
        return
    if kind is str:
        style = "|" if "\n" in value else None
        detected = dumper.resolve(ScalarNode, value, _PLAIN_ONLY)
        dumper.emit(ScalarEvent(None, _STR_TAG, (detected == _STR_TAG, True), value, style=style))
        return
    fixed = _FIXED_SCALARS.get(kind)
    if fixed is not None:
        tag, render = fixed
        dumper.emit(ScalarEvent(None, tag, (True, False), render(value), style=None))
        return
    if kind is float:
        text = _float_text(value)
        detected = dumper.resolve(ScalarNode, text, _PLAIN_ONLY)
        dumper.emit(
            ScalarEvent(None, _FLOAT_TAG, (detected == _FLOAT_TAG, False), text, style=None)
        )
        return
    node = dumper.represent_data(value)
    # represent_data remembers every non-scalar it represents, for aliases; a
    # streaming walk emits no aliases, so the memo would only grow.
    if dumper.represented_objects:
        dumper.represented_objects = {}
        dumper.object_keeper = []
    _emit_node(dumper, node)


def _emit_node(dumper: Any, node: Node) -> None:
    """Emit what ``yaml.serializer.Serializer.serialize_node`` emits for *node*, without anchors."""
    emit = dumper.emit
    if isinstance(node, ScalarNode):
        detected_tag = dumper.resolve(ScalarNode, node.value, (True, False))
        default_tag = dumper.resolve(ScalarNode, node.value, (False, True))
        implicit = (node.tag == detected_tag), (node.tag == default_tag)
        emit(ScalarEvent(None, node.tag, implicit, node.value, style=node.style))
    elif isinstance(node, SequenceNode):
        implicit = node.tag == dumper.resolve(SequenceNode, node.value, True)
        emit(SequenceStartEvent(None, node.tag, implicit, flow_style=node.flow_style))
        for item in node.value:
            _emit_node(dumper, item)
        emit(SequenceEndEvent())
    elif isinstance(node, MappingNode):
        implicit = node.tag == dumper.resolve(MappingNode, node.value, True)
        emit(MappingStartEvent(None, node.tag, implicit, flow_style=node.flow_style))
        for key, item in node.value:
            _emit_node(dumper, key)
            _emit_node(dumper, item)
        emit(MappingEndEvent())
    else:  # pragma: no cover - PyYAML has no other node kind
        raise TypeError(f"unexpected YAML node {type(node).__name__}")
