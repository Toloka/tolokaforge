"""Inspect AI adapter for tolokaforge."""

__all__ = ["InspectAiAdapter"]


def __getattr__(name: str):
    if name == "InspectAiAdapter":
        from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter

        return InspectAiAdapter
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(__all__ + list(globals().keys()))
