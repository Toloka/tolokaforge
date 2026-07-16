"""``ToolRegistry`` — name → :class:`InteractiveTool` plus entry-point discovery."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from importlib.metadata import entry_points

from intervener.tools.base import InteractiveTool

__all__ = ["ToolRegistry"]

_LOG = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "intervener.tools"


class ToolRegistry:
    """Mapping ``tool.name`` → tool instance, with duplicate detection.

    Construct directly for tight control::

        registry = ToolRegistry([ContextTool(), AnalyzeTool()])

    Or auto-discover installed tools plus explicit extras::

        registry = ToolRegistry.with_discovered(MyLocalTool())

    The latter enumerates ``intervener.tools`` entry-points from every
    installed package (``pip install my-pkg`` → next call sees it).
    """

    def __init__(self, tools: Iterable[InteractiveTool] = ()) -> None:
        self._tools: dict[str, InteractiveTool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: InteractiveTool) -> None:
        """Add ``tool`` to the registry. Collision on name raises ``ValueError``."""
        if tool.name in self._tools:
            raise ValueError(
                f"tool name {tool.name!r} already registered "
                f"(existing: {type(self._tools[tool.name]).__name__}, "
                f"new: {type(tool).__name__})"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> InteractiveTool | None:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[InteractiveTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def list_summary(self) -> list[tuple[str, str]]:
        """Return ``[(name, description), …]`` in insertion order — for help text."""
        return [(t.name, t.description) for t in self._tools.values()]

    @classmethod
    def with_discovered(cls, *extras: InteractiveTool) -> ToolRegistry:
        """Instantiate every entry-point in ``intervener.tools``, add extras, return.

        A discovered entry-point that fails to import or instantiate is
        logged and skipped — one broken plug-in must not brick the
        registry for everyone else.
        """
        registry = cls()
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                factory = ep.load()
                registry.register(factory())
            except Exception as exc:
                _LOG.warning(
                    "intervener.tools entry-point %r failed to load: %s",
                    ep.name,
                    exc,
                )
        for extra in extras:
            registry.register(extra)
        return registry
