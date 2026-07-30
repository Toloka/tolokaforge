"""Engine/runner wire-protocol version.

The engine declares :data:`ENGINE_PROTOCOL_VERSION` on every
``RegisterTrialRequest``; the runner refuses to register a trial from an engine
that declares less than its own value. That places the version gate at
registration, before any tokens are spent — a per-call rejection would reach the
agent as an ordinary tool failure, and it would retry until the turn budget was
gone while the trial still reported ``status=completed``.

Version 1 is the first that sends ``ExecuteToolRequest.call_id``. Without it a
tool call cannot be joined to the result it produced, so a runner built from
this tree cannot grade a trial driven by an older engine.
"""

ENGINE_PROTOCOL_VERSION = 1
