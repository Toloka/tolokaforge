"""Test-only :class:`GradingSubstrate` — hand-built canned state.

The dispatch test injects this class into a synthetic entry-point mapping
under ``tolokaforge.grading_substrates`` to prove
:func:`tolokaforge.core.plugin_registry.load_grading_substrate` resolves
third-party substrates by name. The class is deliberately not registered
in ``pyproject.toml``: a real entry-point line would ship a dangling
``tests.utils.*`` reference in the wheel, which is what monkeypatched
discovery lets us avoid.

Every accessor returns a constant snapshot the constructor received; no
network, no filesystem, no DB reads. The Protocol shape is what matters
for the resolution test, not the state values.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch


class DummyGradingSubstrate:
    """Canned-snapshot substrate for the dispatch-loader lock."""

    def __init__(
        self,
        *,
        db_reader: DBReader,
        knowledge_search: KnowledgeSearch | None = None,
        filesystem_root: Path | None = None,
        initial_state: dict[str, Any] | None = None,
        final_state: dict[str, Any] | None = None,
        final_state_stable: dict[str, Any] | None = None,
        filesystem_state: dict[str, str] | None = None,
    ) -> None:
        self._db_reader = db_reader
        self._knowledge_search = knowledge_search
        self._filesystem_root = filesystem_root
        self._initial_state = initial_state or {}
        self._final_state = final_state or {}
        self._final_state_stable = final_state_stable or {}
        self._filesystem_state = filesystem_state

    def db_reader(self) -> DBReader:
        return self._db_reader

    def knowledge_search(self) -> KnowledgeSearch | None:
        return self._knowledge_search

    def filesystem_root(self) -> Path | None:
        return self._filesystem_root

    def initial_state(self) -> dict[str, Any]:
        return self._initial_state

    def final_state(self) -> dict[str, Any]:
        return self._final_state

    def final_state_stable(self) -> dict[str, Any]:
        return self._final_state_stable

    def filesystem_state(self) -> dict[str, str] | None:
        return self._filesystem_state

    def close(self) -> None:
        return None
