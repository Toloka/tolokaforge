"""Id contract v2, shared with the offline bundle uploader (ADR-0047).

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


# -- scores and gradings (shared with the connector, ADR-0047) --------------------------------
#
#     score_id = uuid5(NS, "score|{trace_id}|{scope...}|{name}").hex
#
# A score on a grading observation has the scope ("grading", <grading_id>); the trace-level
# mirror of the primary grading has the scope ("primary",). The grading the run itself wrote is
# ``live:<run_id>``, shared by every trial of the run.

SCORE_SCOPE_GRADING = "grading"
SCORE_SCOPE_PRIMARY = "primary"
GRADING_SOURCE_LIVE = "live"


def live_grading_id(run_id: str) -> str:
    return f"{GRADING_SOURCE_LIVE}:{check_component('run_id', run_id)}"


def grading_observation_id(trace: str, grading_id: str) -> str:
    return observation_id(trace, "grading", grading_id)


def score_id(trace: str, name: str, *, scope: tuple[str, ...] = (SCORE_SCOPE_PRIMARY,)) -> str:
    """32-hex score id under ``scope``."""
    if not _TRACE_SHAPE.match(trace):
        raise ValueError(f"trace id must be 32 lowercase hex characters: {trace!r}")
    if not scope:
        raise ValueError("a score id needs a scope")
    parts = ["score", trace, *(check_component("scope", part) for part in scope)]
    parts.append(check_component("name", name))
    return uuid.uuid5(NAMESPACE, "|".join(parts)).hex


def grading_score_id(trace: str, grading_id: str, name: str) -> str:
    return score_id(trace, name, scope=(SCORE_SCOPE_GRADING, grading_id))


def primary_score_id(trace: str, name: str) -> str:
    return score_id(trace, name, scope=(SCORE_SCOPE_PRIMARY,))
