"""wire_probes merged domain tool modules (records + open_object + telecom + registry)."""

from tolokaforge.core.tools_interface import DomainToolRegistry


def register_all(registry: DomainToolRegistry) -> None:
    """Register every wire_probes tool with *registry*."""
    from .open_object import register as _open_object
    from .records import register as _records
    from .registry import register as _registry
    from .telecom import register as _telecom

    _records(registry)
    _open_object(registry)
    _telecom(registry)
    _registry(registry)
