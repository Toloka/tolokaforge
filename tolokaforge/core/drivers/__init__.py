"""Shipped :class:`~tolokaforge.core.agent_driver.AgentDriver` implementations.

Each module holds one driver. Adding a new mode (hybrid multi-model, an
in-process agent loop, another vendor family) is a new module here — no
adapter class needs to change.
"""

from __future__ import annotations

from .coding_harness import CodingHarnessDriver

__all__ = ["CodingHarnessDriver"]
