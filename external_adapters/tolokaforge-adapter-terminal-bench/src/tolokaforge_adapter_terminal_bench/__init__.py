"""Terminal-bench adapter for tolokaforge."""

__all__ = ["TerminalBenchAdapter"]


def __getattr__(name: str):
    if name == "TerminalBenchAdapter":
        from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

        return TerminalBenchAdapter
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(__all__ + list(globals().keys()))
