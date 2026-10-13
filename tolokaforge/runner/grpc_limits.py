"""The one gRPC message-size bound shared by the engine, the runner and the grader.

A trial's whole JSON-DB world crosses gRPC in single unary messages: the
``RegisterTrial`` request carries the initial state inline, ``GetState`` and
the grader's ``SubstrateService`` reads return the whole final state, and the
grader's ``Grade`` request carries the task description (initial state
included). Every data-carrying endpoint therefore applies the same bound on
both ends, configured once as ``compute.grpc_max_message_mb`` in the run
config.

Host-side clients receive the value directly from the run config. Containers
(the runner and the grader) read it from :data:`MAX_MESSAGE_ENV_VAR`, which the
engine sets on every runner container it starts or materialises from a
task-declared compose file; an operator-managed grader container sets it in
its own deployment.

This module imports nothing beyond the standard library so the run-config
schema, the runner image and the grader image can all share it.
"""

from __future__ import annotations

import os

__all__ = [
    "DEFAULT_MAX_MESSAGE_MB",
    "MAX_MESSAGE_ENV_VAR",
    "MAX_MESSAGE_MB_CEILING",
    "MAX_MESSAGE_MB_FLOOR",
    "MAX_MESSAGE_SETTING",
    "channel_options",
    "max_message_mb_from_env",
    "resource_exhausted_message",
    "validate_max_message_mb",
]

DEFAULT_MAX_MESSAGE_MB = 50
"""Default bound in MiB, applied on both ends of every data-carrying channel."""

MAX_MESSAGE_MB_FLOOR = 4
"""Lowest accepted bound in MiB — gRPC's own default receive limit. A lower
value would only make the engine refuse worlds the transport accepts."""

MAX_MESSAGE_MB_CEILING = 2047
"""Highest accepted bound in MiB. gRPC's message-length options are signed
32-bit byte counts, so 2047 MiB is the largest whole-MiB value that fits."""

MAX_MESSAGE_ENV_VAR = "TOLOKAFORGE_GRPC_MAX_MESSAGE_MB"
"""Environment variable the runner and grader containers read the bound from."""

MAX_MESSAGE_SETTING = "compute.grpc_max_message_mb"
"""The run-config path an operator raises when a message exceeds the bound."""

_BYTES_PER_MIB = 1024 * 1024


def validate_max_message_mb(value: int) -> int:
    """Return ``value`` when it is a whole number of MiB inside the bounds.

    Raises:
        ValueError: ``value`` is not an ``int`` (``bool`` included) or lies
            outside ``[MAX_MESSAGE_MB_FLOOR, MAX_MESSAGE_MB_CEILING]``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"gRPC max message size must be a whole number of MiB; got {value!r}.")
    if not MAX_MESSAGE_MB_FLOOR <= value <= MAX_MESSAGE_MB_CEILING:
        raise ValueError(
            f"gRPC max message size must be between {MAX_MESSAGE_MB_FLOOR} and "
            f"{MAX_MESSAGE_MB_CEILING} MiB; got {value}."
        )
    return value


def channel_options(max_message_mb: int) -> list[tuple[str, int]]:
    """gRPC server / channel options bounding sent and received messages.

    The same options apply to ``grpc.server(options=...)`` and
    ``grpc.insecure_channel(target, options=...)``.
    """
    limit_bytes = validate_max_message_mb(max_message_mb) * _BYTES_PER_MIB
    return [
        ("grpc.max_send_message_length", limit_bytes),
        ("grpc.max_receive_message_length", limit_bytes),
    ]


def max_message_mb_from_env() -> int:
    """Read the bound from :data:`MAX_MESSAGE_ENV_VAR` inside a container.

    Unset (or empty) means :data:`DEFAULT_MAX_MESSAGE_MB`. A value that is
    set but not a whole number in the accepted range raises instead of
    falling back, so a misconfigured container fails at startup rather than
    serving with a bound its peer does not share.

    Raises:
        ValueError: the variable is set to a non-integer or an out-of-range
            value; the message names the variable and the accepted range.
    """
    raw = os.environ.get(MAX_MESSAGE_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MAX_MESSAGE_MB
    invalid = ValueError(
        f"{MAX_MESSAGE_ENV_VAR}={raw!r} is invalid: expected a whole number of MiB "
        f"between {MAX_MESSAGE_MB_FLOOR} and {MAX_MESSAGE_MB_CEILING}."
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise invalid from exc
    if not MAX_MESSAGE_MB_FLOOR <= value <= MAX_MESSAGE_MB_CEILING:
        raise invalid
    return value


def resource_exhausted_message(operation: str, max_message_mb: int, detail: str) -> str:
    """Actionable error text for a call that exceeded a message bound.

    ``operation`` names the RPC; ``detail`` carries gRPC's own status
    details, which state the observed size and the bound that refused it.
    """
    return (
        f"{operation} failed: the message exceeds the gRPC message size limit "
        f"({detail}). The engine's limit is {max_message_mb} MiB, set by "
        f"`{MAX_MESSAGE_SETTING}`; raise it in the run config (at most "
        f"{MAX_MESSAGE_MB_CEILING} MiB). A runner or grader deployed outside the engine "
        f"needs the same value in {MAX_MESSAGE_ENV_VAR}."
    )
