"""Task-scope system prompt assembly — pure, side-effect-free, HTTP-free.

Produces the agent system prompt (pre-policy) the first
:meth:`LLMClient.generate` receives on ``system=``. The priority chain
walks the task authoring surfaces from most specific to fallback:

1. ``task.policies["agent_system_prompt"]`` — inline string, returned
   verbatim.
2. ``task.system_prompt`` names a file under *task_dir* — file contents
   returned verbatim.
3. Legacy ``main_policy.md`` alongside an additional-policy file —
   composed under ``<main_policy>`` / ``<tech_support_policy>`` and
   wrapped in an ``<instructions>`` / ``<policy>`` envelope.
4. Minimal default with ``policies["guidance"]`` bullets and, when
   present, ``tools.agent.browser.initial_url``.

The only side effect is reading local files. The returned string is
handed to the prompt policy layer for enrichment before the wire call.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.core.models import TaskConfig

__all__ = ["build_system_prompt"]


_AGENT_INSTRUCTION_WITH_TRAILING_NEWLINE = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy.
"""

_AGENT_INSTRUCTION_NO_TRAILING_NEWLINE = _AGENT_INSTRUCTION_WITH_TRAILING_NEWLINE.rstrip("\n")


def _wrap_policy_document(agent_instruction: str, policy_body: str) -> str:
    return (
        f"<instructions>\n{agent_instruction}\n</instructions>\n<policy>\n{policy_body}\n</policy>"
    )


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


def build_system_prompt(*, task: TaskConfig, task_dir: Path) -> str:
    """Assemble the pre-policy agent system prompt for *task*.

    Priority (first-match-wins):

    1. ``task.policies["agent_system_prompt"]`` — inline string, returned
       verbatim.
    2. ``task.system_prompt`` as a filename in *task_dir* — file contents
       returned verbatim.
    3. Legacy ``main_policy.md`` alongside an additional-policy file —
       composed into ``<main_policy>`` / ``<tech_support_policy>``
       sections under an ``<instructions>`` / ``<policy>`` envelope.
    4. Minimal default that lists any ``policies["guidance"]`` bullets
       and, when present, ``tools.agent.browser.initial_url``.

    Deterministic. Only side effect is local-file reads. Never opens a
    network connection.
    """
    if "agent_system_prompt" in task.policies:
        return task.policies["agent_system_prompt"]

    if task.system_prompt:
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
