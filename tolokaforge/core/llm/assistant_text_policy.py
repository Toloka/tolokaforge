"""Assistant-text reshaping slot.

Some providers wrap the assistant's textual response in structured markers
before it reaches ``message.content`` on the wire (Cohere's Command-A+ family
emits ``<|START_TEXT|>…<|END_TEXT|>`` delimiters around the reply; other
providers may prepend/append tokens that leak the model's chat template).
:class:`AssistantTextPolicy` is the extension slot that reshapes the text
between litellm's parse and :class:`GenerationResult.text` so the string that
ends up in ``trajectory.yaml``, transcript graders, and LLM-judge input is
free of provider artefacts.

Extension contract
------------------
:class:`AssistantTextPolicy` is a runtime-checkable Protocol with a single
method:

.. code-block:: python

    def parse_assistant_text(
        self, text: str, *, model_config: ModelConfig
    ) -> str: ...

The full ``text`` string is passed unmodified — the policy sees the provider's
output as :class:`~tolokaforge.core.llm.client.LLMClient._assemble_result`
received it, not a pre-digested slice. :class:`ModelConfig` is threaded through
so a subclass can dispatch on the resolved model identity (name, provider,
capability overrides) rather than requiring a separate class per model.

Two implementations conceptually exist; only :class:`PassthroughAssistantText`
ships in-engine:

* :class:`PassthroughAssistantText` — no-op; the default for every preset.
  Every provider we ship a preset for today puts the response verbatim on
  ``message.content``.
* A per-provider marker-stripping subclass — expressible via the seam
  without engine edits. The Cohere ``<|START_TEXT|>…<|END_TEXT|>`` case
  is covered by a fixture-scope subclass in the unit test
  ``tests/unit/llm/test_assistant_text_policy_seam.py``; a shipped
  Cohere policy ships alongside the Cohere preset when the family lands.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tolokaforge.core.models.model_config import ModelConfig

__all__ = [
    "AssistantTextPolicy",
    "PassthroughAssistantText",
]


@runtime_checkable
class AssistantTextPolicy(Protocol):
    """Reshapes assistant text between litellm parse and ``GenerationResult``.

    Called on every completion:
    :meth:`~tolokaforge.core.llm.client.LLMClient._assemble_result` invokes
    :meth:`parse_assistant_text` on the raw ``message.content`` before the
    result flows into ``trajectory.yaml`` and downstream graders. The mock-
    generator path is deliberately excluded from this call so offline tests
    stay policy-agnostic.
    """

    def parse_assistant_text(self, text: str, *, model_config: ModelConfig) -> str: ...


class PassthroughAssistantText:
    """Default policy — return the assistant text unchanged.

    Every shipped preset resolves to this class so wire output is
    byte-identical to pre-slot behaviour. A subclass overrides
    :meth:`parse_assistant_text` when the provider needs marker stripping
    or other reshaping.
    """

    def parse_assistant_text(self, text: str, *, model_config: ModelConfig) -> str:  # noqa: ARG002
        return text
