"""Demo conversion adapter — a canonical-test fixture package.

Installed into a throwaway scratch venv alongside the built ``tolokaforge``
wheel so the entry-point discovery smoke test exercises the production
``importlib.metadata`` adapter-load path against a genuinely separate
distribution. Never a uv workspace member and never packaged into the
``tolokaforge`` wheel.
"""

__all__ = ["DemoConversionAdapter"]


def __getattr__(name: str):
    if name == "DemoConversionAdapter":
        from tolokaforge_adapter_demo.adapter import DemoConversionAdapter

        return DemoConversionAdapter
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted(__all__ + list(globals().keys()))
