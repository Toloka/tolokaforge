"""Langfuse live tracing for tolokaforge (ADR-0047, packaging amendment).

This wheel ships the OTLP trial observer, the trial-end projection of a persisted trial bundle,
the attachment step, the deployment profile and the model-name resolution the engine's
``TrialObserver`` seam is fed with. The engine finds it through the ``tolokaforge.trial_observers``
entry-point group (``tolokaforge_langfuse.plugin:build``); it imports no engine module at load
time, so ``import tolokaforge_langfuse`` works wherever the wheel is installed and the two
constants below can be read without the engine.
"""

from __future__ import annotations

from typing import Final

__all__ = ["__api_version__", "__version__"]

__version__: Final[str] = "0.1.0"
"""PEP 440 version of this wheel."""

__api_version__: Final[int] = 3
"""The trial-observer plugin contract this wheel speaks; the engine's
``tolokaforge.observability.factory.PLUGIN_API_VERSION`` must equal it (checked at run start)."""
