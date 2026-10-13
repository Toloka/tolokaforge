"""Task-scope system prompt assembly — pure, side-effect-free, HTTP-free.

Produces the agent system prompt (pre-policy) the first
:meth:`LLMClient.generate` receives on ``system=``. An inline
``task.policies["agent_system_prompt"]`` short-circuits everything and is
returned verbatim. Otherwise the prompt is a reply contract — how to answer,
from :mod:`tolokaforge.core.agent_prompt_contract`, present only when one is
selected — followed by the task's own document, which the authoring chain
walks from most specific to fallback:

1. ``task.system_prompt`` names a file under *task_dir* — file contents
   returned verbatim.
2. Legacy ``main_policy.md`` alongside an additional-policy file —
   composed under ``<main_policy>`` / ``<tech_support_policy>`` and
   wrapped in an ``<instructions>`` / ``<policy>`` envelope.
3. Minimal default with ``policies["guidance"]`` bullets and, when
   present, ``tools.agent.browser.initial_url``.

The only side effect is reading local files. The returned string is
handed to the prompt policy layer for enrichment before the wire call.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from tolokaforge.core.agent_prompt_contract import (
    observation_window_clause,
    resolve_agent_prompt_contract,
)

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


_BARE_PERSONA = "You are a helpful assistant."


def _build_minimal_default(task: TaskConfig, *, persona: bool = True) -> str:
    """Guidance bullets and browser hint, under a generic persona.

    *persona* is dropped when a reply contract already opened the prompt with
    one of its own: two personas in one prompt contradict each other, and the
    contract's is the specific one.
    """
    parts = [_BARE_PERSONA] if persona else []

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


def _compose_with_contract(
    contract: str, body: str | None, *, observation_window: int | None
) -> str:
    """Put the reply contract first, the task's own prompt after it.

    Order is deliberate: the contract describes how to answer every turn and
    stays true for the whole episode, while the body describes this particular
    job. A model reading top-down meets the standing rule before the specifics.

    When *observation_window* is set the contract is followed by
    :func:`~tolokaforge.core.agent_prompt_contract.observation_window_clause`,
    the one statement about output retention the loop makes true. With no
    window every observation is replayed in full and nothing is added.
    """
    if observation_window is not None:
        contract = f"{contract}\n\n{observation_window_clause(observation_window)}"
    if body is None or not body.strip():
        return contract
    return f"{contract}\n\n{body}"


def build_system_prompt(
    *,
    task: TaskConfig,
    task_dir: Path,
    default_prompt_contract: str | None = None,
    observation_window: int | None = None,
) -> str:
    """Assemble the pre-policy agent system prompt for *task*.

    An inline ``task.policies["agent_system_prompt"]`` wins outright and is
    returned verbatim. Otherwise the result is the selected reply contract,
    if any, followed by the task's own document as resolved by
    :func:`_build_task_body`.

    *default_prompt_contract* is the model preset's
    ``default_agent_prompt_contract``. ``task.agent_prompt_contract`` names a
    contract over it, and it applies only under
    ``interaction_mode: agent_only``: the shipped text tells an agent that a
    tool-call-free message ends the task, which is true of the solo turn
    policy and false of a conversation with a user.

    *observation_window* is the model's ``observation_window`` capability, the
    same value the loop collapses older observations by. A contract composed
    for a run that drops output says so, with the number; one composed for a
    run that keeps everything says nothing about it.

    Deterministic. Only side effect is local-file reads. Never opens a
    network connection.
    """
    if "agent_system_prompt" in task.policies:
        return task.policies["agent_system_prompt"]

    contract_selector = task.agent_prompt_contract
    if contract_selector is None and task.interaction_mode == "agent_only":
        contract_selector = default_prompt_contract
    contract = (
        resolve_agent_prompt_contract(contract_selector, task_dir=task_dir)
        if contract_selector
        else None
    )

    if contract is not None:
        body = _build_task_body(task=task, task_dir=task_dir, persona=False)
        return _compose_with_contract(contract, body, observation_window=observation_window)

    return _build_task_body(task=task, task_dir=task_dir)


def _build_task_body(*, task: TaskConfig, task_dir: Path, persona: bool = True) -> str:
    """The task's own prompt, by the authoring chain that predates contracts."""
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

    return _build_minimal_default(task, persona=persona)
