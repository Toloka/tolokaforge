"""Reference :class:`~intervener.protocols.EventSink` implementations."""

from intervener.sinks.compound import CompoundSink
from intervener.sinks.jsonl import JsonlSink
from intervener.sinks.plain import PlainLineSink
from intervener.sinks.rich_console import RichConsoleSink
from intervener.sinks.silent import SilentSink

__all__ = [
    "CompoundSink",
    "JsonlSink",
    "PlainLineSink",
    "RichConsoleSink",
    "SilentSink",
]
