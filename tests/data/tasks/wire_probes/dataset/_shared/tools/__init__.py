"""wire_probes merged domain tool modules (records + samples + telecom + registry)."""

from tolokaforge.core.tools_interface import DomainToolRegistry


def register_all(registry: DomainToolRegistry) -> None:
    """Register every wire_probes tool with *registry*."""
    from .records import register as _records
    from .registry import register as _registry
    from .samples import register as _samples
    from .telecom import register as _telecom

    _records(registry)
    _samples(registry)
    _telecom(registry)
    _registry(registry)
