"""Harbor-harness adapter for tolokaforge."""

__all__ = ["HarborAdapter"]


def __getattr__(name: str):
    if name == "HarborAdapter":
        from tolokaforge_adapter_harbor.adapter import HarborAdapter

        return HarborAdapter
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(__all__ + list(globals().keys()))
