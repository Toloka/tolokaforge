"""Task-scope system prompt assembly — pure, side-effect-free, HTTP-free.

Produces the agent system prompt (pre-policy) the first
:meth:`LLMClient.generate` receives on ``system=``. The priority chain
walks the task authoring surfaces from most specific to fallback:

1. ``task.policies["agent_system_prompt"]`` — inline string.
2. ``task.system_prompt == "__adapter__"`` — delegate to
   :meth:`BaseAdapter.get_system_prompt` and wrap in ``<policy>``.
3. ``task.system_prompt`` (file path) — read verbatim.
4. Legacy ``main_policy.md`` + additional-policy split — two files
   composed under ``<main_policy>`` / ``<tech_support_policy>``.
5. Minimal default with ``policies["guidance"]`` + optional
   ``tools.agent.browser.initial_url``.

The only side effect is reading local files. The returned string is
handed to the prompt policy layer for enrichment before the wire call.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.adapters import BaseAdapter
    from tolokaforge.core.models import TaskConfig

__all__ = ["build_system_prompt"]


_AGENT_INSTRUCTION_WITH_TOOLS = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy.
"""

_AGENT_INSTRUCTION_NO_TRAILING_NEWLINE = _AGENT_INSTRUCTION_WITH_TOOLS.rstrip("\n")


def _wrap_policy_document(agent_instruction: str, policy_body: str) -> str:
    return (
        f"<instructions>\n{agent_instruction}\n</instructions>\n<policy>\n{policy_body}\n</policy>"
    )


def _build_from_adapter(adapter_prompt: str) -> str:
    return _wrap_policy_document(_AGENT_INSTRUCTION_WITH_TOOLS, adapter_prompt)


def _build_legacy_main_policy(main_policy: str, additional_policy: str | None) -> str:
    if additional_policy is not None:
        domain_policy = (
            "<main_policy>\n"
            + main_policy
            + "\n</main_policy>\n"
            + "<tech_support_policy>\n"
            + additional_policy
            + "\n</tech_support_policy>"
        )
    else:
        domain_policy = main_policy
    return _wrap_policy_document(_AGENT_INSTRUCTION_NO_TRAILING_NEWLINE, domain_policy)


def _build_single_file(domain_policy: str) -> str:
    return _wrap_policy_document(_AGENT_INSTRUCTION_NO_TRAILING_NEWLINE, domain_policy)


def _build_minimal_default(task: TaskConfig) -> str:
    parts = ["You are a helpful assistant."]

    guidance = task.policies.get("guidance", []) if task.policies else []
    if guidance:
        parts.append("\nGuidance:")
        for g in guidance:
            parts.append(f"- {g}")

    browser_config = task.tools.agent.get("browser", {}) if task.tools else {}
    if isinstance(browser_config, dict):
        browser_url = browser_config.get("initial_url")
        if browser_url:
            parts.append(f"\nThe web portal is available at: {browser_url}")
            parts.append(
                "Navigate to this URL to access the portal content. "
                "Do not guess other URLs or ports."
            )

    return "\n".join(parts)


def build_system_prompt(
    *,
    task: TaskConfig,
    task_dir: Path,
    adapter: BaseAdapter | None,
) -> str:
    """Assemble the pre-policy agent system prompt for *task*.

    Priority (first-match-wins):

    1. ``task.policies["agent_system_prompt"]`` — inline string.
    2. ``task.system_prompt == "__adapter__"`` and *adapter* is set —
       wraps :meth:`BaseAdapter.get_system_prompt` output in an
       instructions / policy envelope. Falls through when the adapter
       returns an empty value.
    3. ``task.system_prompt`` as a filename in *task_dir* — file
       contents returned verbatim.
    4. Legacy ``main_policy.md`` alongside an additional-policy file —
       composed into ``<main_policy>`` / ``<tech_support_policy>``
       sections under the same envelope.
    5. Minimal default that lists any ``policies["guidance"]`` bullets
       and, when present, ``tools.agent.browser.initial_url``.

    Deterministic. Only side effect is local-file reads. Never opens a
    network connection.
    """
    if "agent_system_prompt" in task.policies:
        return task.policies["agent_system_prompt"]

    if task.system_prompt == "__adapter__" and adapter is not None:
        adapter_prompt = adapter.get_system_prompt(task.task_id)
        if adapter_prompt:
            return _build_from_adapter(adapter_prompt)

    if task.system_prompt and task.system_prompt != "__adapter__":
        system_prompt_path = task_dir / task.system_prompt
        if system_prompt_path.exists():
            return system_prompt_path.read_text()

    main_policy_path = task_dir.parent / "main_policy.md"
    if not main_policy_path.exists():
        main_policy_path = task_dir / "main_policy.md"

    if main_policy_path.exists() and task.system_prompt:
        main_policy = main_policy_path.read_text()

        additional_policy_path = task_dir.parent / task.system_prompt
        if not additional_policy_path.exists():
            additional_policy_path = task_dir / task.system_prompt

        additional_policy: str | None = None
        if additional_policy_path.exists():
            additional_policy = additional_policy_path.read_text()

        return _build_legacy_main_policy(main_policy, additional_policy)

    if task.system_prompt:
        system_prompt_path = task_dir / task.system_prompt
        if system_prompt_path.exists():
            return _build_single_file(system_prompt_path.read_text())

    return _build_minimal_default(task)
