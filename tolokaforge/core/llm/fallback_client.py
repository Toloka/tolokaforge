"""Ordered per-trial model-fallback wrapper around :class:`LLMClient`.

Contract — ordered, not round-robin. Each :class:`FallbackLLMClient`
instance owns a chain ``[primary, *fallbacks]`` and a cursor starting at
zero. Every :meth:`generate` call delegates to the cursor-current
:class:`LLMClient`; on any exception the cursor advances one step in the
chain and the call retries once against the new client. The cursor never
rewinds — a fallback that started serving turns for this trial keeps
serving them. Chain-exhausted → the final exception propagates.

Rate-limit-style transient errors reach this wrapper only after
:class:`LLMClient`'s own tenacity ``@retry(stop_after_attempt=5)`` has
given up — the fallback only fires on what the primary itself declared
unrecoverable, avoiding provider swaps on every 429.

The wrapper mirrors :class:`LLMClient`'s public attribute surface (``config``
and ``capabilities``) via forwarding properties, so :class:`ConductorContext`
consumers reading ``self.agent_client.config`` /
``self.agent_client.capabilities.schema_sanitizer`` continue to work
transparently as the cursor advances.
"""

from __future__ import annotations

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import GenerationResult, LLMClient
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import ModelConfig

__all__ = ["FallbackLLMClient"]


class FallbackLLMClient:
    """Ordered chain of :class:`LLMClient` instances behind one ``generate`` surface.

    Duck-types :class:`LLMClient`: ``config`` and ``capabilities`` forward
    to the cursor-current client, and ``generate`` transparently swaps
    clients on failure. Constructed once per trial by the orchestrator so
    each trial's cursor is independent.
    """

    def __init__(
        self,
        *,
        primary: ModelConfig,
        fallbacks: list[ModelConfig],
    ) -> None:
        if not fallbacks:
            raise ValueError("FallbackLLMClient requires at least one fallback model")
        self._chain: list[ModelConfig] = [primary, *fallbacks]
        self._cursor: int = 0
        self._current: LLMClient = LLMClient(self._chain[0])
        self._logger = get_logger("tolokaforge.core.llm.fallback")

    @property
    def config(self) -> ModelConfig:
        return self._current.config

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._current.capabilities

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def chain(self) -> tuple[ModelConfig, ...]:
        return tuple(self._chain)

    def generate(self, *args: object, **kwargs: object) -> GenerationResult:
        """Delegate to the current client; advance-and-retry on any raise.

        The final client's exception propagates unchanged so the
        orchestrator's ``wait`` loop classifies it identically to the
        non-fallback case.
        """
        while True:
            try:
                return self._current.generate(*args, **kwargs)  # type: ignore[arg-type]
            except Exception as exc:
                if self._cursor + 1 >= len(self._chain):
                    raise
                from_model = self._chain[self._cursor]
                self._cursor += 1
                to_model = self._chain[self._cursor]
                self._logger.warning(
                    "Fallback triggered",
                    from_provider=from_model.provider,
                    from_name=from_model.name,
                    to_provider=to_model.provider,
                    to_name=to_model.name,
                    cursor=self._cursor,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                self._current = LLMClient(to_model)
