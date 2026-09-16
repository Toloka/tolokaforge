"""Id contract v2, shared with the offline bundle uploader (ADR-0046).

    trace_id       = uuid5(NS, "trace|{run_tag}|{run_id}|{task_id}|{trial_index}|{attempt}").hex
    observation_id = uuid5(NS, "obs|{trace_id}|{kind}|{key...}").hex[:16]

The same trial maps to the same trace and observation ids whether it is exported live (this
package) or replayed from its bundle, so a re-send from either side updates instead of
duplicating. Every observation kind has a **stable key** that is a fact of the trial, never a
position in a filtered list: ``root`` uses the literal ``-``; ``gen`` (agent turn) and ``ugen``
(simulated user turn) the message index in the recorded trajectory; ``tool`` the episode-unique
tool-call id the loop assigned (``msg:<index>`` when there is none); ``grading`` a grading id;
``jgen`` / ``jtool`` the grading id plus the judge message index or call id; ``event`` a
source-qualified key (``log:<i>``, ``guard:<i>``, ...). No component may be empty, carry
surrounding whitespace or contain ``|``. The namespace never changes; a change of the formula is
a new contract version.
"""

from __future__ import annotations

import re
import uuid

NAMESPACE = uuid.UUID("00000000-0000-0000-0000-00000000f00d")
CONTRACT_VERSION = 2
DEFAULT_RUN_TAG = "v1"
ROOT_KEY = "-"
OBSERVATION_KINDS = frozenset({"root", "gen", "ugen", "tool", "grading", "jgen", "jtool", "event"})

_TRACE_SHAPE = re.compile(r"^[0-9a-f]{32}$")


def check_component(name: str, value: object) -> str:
    """One id component as text: non-empty, no surrounding whitespace, no ``|``."""
    text = str(value)
    if not text or text != text.strip():
        raise ValueError(
            f"id component {name!r} must be non-empty without surrounding whitespace: {text!r}"
        )
    if "|" in text:
        raise ValueError(f"id component {name!r} must not contain '|': {text!r}")
    return text


def trace_id(
    *, run_tag: str, run_id: str, task_id: str, trial_index: object, attempt: object
) -> str:
    """32-hex trace id (a 128-bit OTLP trace id)."""
    name = "|".join(
        (
            "trace",
            check_component("run_tag", run_tag),
            check_component("run_id", run_id),
            check_component("task_id", task_id),
            check_component("trial_index", trial_index),
            check_component("attempt", attempt),
        )
    )
    return uuid.uuid5(NAMESPACE, name).hex


def observation_id(trace: str, kind: str, *key: object) -> str:
    """16-hex observation id (a 64-bit OTLP span id) for ``kind`` under its stable ``key``."""
    if kind not in OBSERVATION_KINDS:
        raise ValueError(
            f"unknown observation kind {kind!r}; expected one of {sorted(OBSERVATION_KINDS)}"
        )
    if not _TRACE_SHAPE.match(trace):
        raise ValueError(f"trace id must be 32 lowercase hex characters: {trace!r}")
    if not key:
        raise ValueError(f"observation kind {kind!r} needs a stable key")
    parts = ["obs", trace, kind, *(check_component(f"{kind} key", part) for part in key)]
    return uuid.uuid5(NAMESPACE, "|".join(parts)).hex[:16]


def tool_key(call_id: object | None, message_index: object) -> str:
    """The stable key of a tool execution: the loop's call id, else ``msg:<index>``."""
    if call_id not in (None, ""):
        return str(call_id)
    return f"msg:{message_index}"
