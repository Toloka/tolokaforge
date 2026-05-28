"""Notes tool modules."""

from tolokaforge.core.tools_interface import DomainToolRegistry


def register_all(registry: DomainToolRegistry) -> None:
    """Register all notes tools with *registry*."""
    from .notes import register as _notes

    _notes(registry)
