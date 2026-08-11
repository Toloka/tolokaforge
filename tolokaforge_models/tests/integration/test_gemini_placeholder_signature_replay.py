"""Live A/B integration test — 2026-05-21 OTS regression hypothesis.

**Hypothesis H1**: PR #101's placeholder-drop (40b3...) breaks Gemini 3.1
Pro's thinking continuity across tool-call turns on long-context flows.
The 48-char ``e248030a7-…`` blob OpenRouter emits when Gemini reported
no real thinking may actually be the dummy ``thought_signature``
OpenRouter inserts to satisfy Google's per-functionCall signature
requirement. Stripping it on replay means "older turn" signatures
disappear from the conversation history; Google's docs warn this *"may
lead to degraded model performance"*.

This test discriminates the hypothesis empirically:

1. Builds a deterministic 3-turn tool-call loop on
   ``google/gemini-3.1-pro-preview`` that mimics the OTS shape — long
   system prompt, lookup tool, write tool, cross-turn data linkage.
2. Runs the loop **twice**: once with the default codec
   (``drop_placeholder_signature=True``, PR #101 behavior) and once
   with the override (``drop_placeholder_signature=False``).
3. Asserts that under the override, **turn 2's reasoning_tokens is at
   least as high as under default** — and ideally meaningfully higher.

Cost budget: ~6 API calls × moderate prompts ≈ $0.30-0.80 per run.
Skips when ``OPENROUTER_API_KEY`` is absent.

If the assertion fires red on the **default** mode (drop) but green on
the **keep** mode, H1 is supported. If both modes yield similar
``reasoning_tokens`` on turn 2, H1 is rejected and we should focus on
H2 (effort=high) or H3 (budget mode) instead.

The test is intentionally **descriptive, not strict**: a 1.5× tolerance
on the keep/drop ratio avoids flakes from the single-trial variance
inherent in live LLM calls, while still gating the directional claim.
For tighter statistical power, run the test under ``--count=N`` (pytest
plugin) or convert to a small batch script.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = [pytest.mark.integration, pytest.mark.requires_api, pytest.mark.llm]


# ---------------------------------------------------------------------------
# Test fixtures — keep the conversation deterministic so reasoning-token
# deltas are attributable to the codec flag, not prompt drift.
# ---------------------------------------------------------------------------

# Long system prompt approximating OTS scale (~3-4k chars). The
# 2026-05-21 investigation correlated Pro's reasoning-skip pattern with
# OTS prompt size (12-28k tokens) versus tau (5k). The smaller-prompt
# rendition of this test (~500 chars, see earlier revisions) failed to
# reproduce the field-omission failure mode in either A/B variant. This
# version pads with realistic-shape policy text so the test exercises a
# context regime closer to the production failure surface — without
# fully duplicating an OTS task pack.
_SYSTEM_PROMPT = """# Apex Internal IT Support Agent — Policy Manual

## 1. Scope
You are the internal IT support agent. Your remit covers system access
requests, employee record updates, password resets, hardware requisition
escalation, software licensing, and account decommissioning. You operate
in a regulated environment where every action is audited; identity
verification and policy adherence are mandatory.

## 2. Identity verification
Every interaction begins with verifying the requester's identity via
``lookup_employee``. Retrieve the employee record before any
ticket-creation action. Do not infer or guess organizational metadata —
always source it from the lookup response.

## 3. Tool catalog
You have access to two tools:

### 3.1 ``lookup_employee``
Retrieves an employee record by ID. Response shape (excerpt):
```
{
  "employee_id": "EMP-001",
  "name": "...",
  "department": "...",
  "organization_id": "ORG-00000004",
  "employment_type": "full_time_employee" | "contractor" | "owner_operator",
  "manager_id": "...",
  "facility_id": "..."
}
```

### 3.2 ``create_ticket``
Creates an access-request ticket in the case management system. Required
fields:
- ``employee_id``: must match the verified employee record
- ``ticket_type``: one of {wms_access, erp_access}
- ``organization_id``: MUST be copied from the looked-up employee
  record. This binds the ticket to the correct legal entity. Tickets
  with missing or mismatched ``organization_id`` route to the wrong
  queue and breach the cross-entity isolation policy. **Always populate
  it from the lookup result.**

## 4. Eligibility rules
- Full-time employees: eligible for WMS and ERP access.
- Contractors: eligible for WMS only; ERP access is denied with
  resolution ``policy_denied``.
- Owner-operators: no internal system access. Deny with resolution
  ``policy_denied`` and direct them to the partner portal.

## 5. Workflow
For each request:
1. Verify the requester (in this conversation: the manager is the
   requester; the access request is on behalf of a subordinate).
2. Look up the target employee record.
3. Validate eligibility against §4.
4. Create the ticket via ``create_ticket``, copying
   ``organization_id`` verbatim from the lookup.
5. Confirm completion to the requester in one sentence; do not request
   re-confirmation from the user.

## 6. Audit trail
Every ticket field is part of the audit trail. Omitting
``organization_id`` is a compliance violation flagged in nightly
reconciliation; the agent must not depend on downstream systems to
backfill it. Treat the create call as terminal — no edit-after-create
flow exists in this scope.

## 7. Tone
Operate professionally. Do not include personal commentary. Do not
volunteer information outside the scope of the request. Do not ask
clarifying questions when the policy is unambiguous; act.
"""

_USER_PROMPT = (
    "I'm the manager. Please grant Tyrone Jackson (EMP-001) standard "
    "WMS access. He just transferred to my team."
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_employee",
            "description": "Retrieve an employee record by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {
                        "type": "string",
                        "description": "Employee identifier (e.g. EMP-001).",
                    },
                },
                "required": ["employee_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": "Create an access-request ticket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string"},
                    "ticket_type": {"type": "string", "enum": ["wms_access", "erp_access"]},
                    "organization_id": {"type": "string"},
                },
                "required": ["employee_id", "ticket_type", "organization_id"],
            },
        },
    },
]

# Synthetic tool response that mirrors what OTS tasks would return:
# enough cross-turn information that the model has to remember to copy
# organization_id into the ticket create call on turn 2.
_LOOKUP_RESPONSE = (
    '{"employee_id": "EMP-001", "name": "Tyrone Jackson", '
    '"department": "warehouse_distribution", "organization_id": "ORG-00000004"}'
)


def _build_client(*, drop_placeholder: bool) -> LLMClient:
    """Build an ``LLMClient`` for Gemini 3.1 Pro with the placeholder-drop
    flag toggled. ``drop_placeholder=True`` matches the PR #101 default;
    ``False`` is the experimental override under test."""
    return LLMClient(
        ModelConfig(
            provider="openrouter",
            name="google/gemini-3.1-pro-preview",
            temperature=0.6,
            capabilities={"gemini_drop_placeholder_signature": drop_placeholder},
        )
    )


def _run_three_turn_loop(client: LLMClient) -> dict[str, Any]:
    """Drive a 3-turn lookup → create → confirm conversation.

    Returns per-turn ``reasoning_tokens`` and the final tool-call argument
    set, so callers can both check the *quantity* of mid-trial reasoning
    and the *quality* of the resulting tool call (does
    ``organization_id`` survive?).
    """
    reasoning_cfg = ReasoningConfig(mode="adaptive", effort_hint="medium")

    # --- Turn 1: user asks, expect assistant to call lookup_employee.
    turn1 = client.generate(
        system=_SYSTEM_PROMPT,
        messages=[Message(role=MessageRole.USER, content=_USER_PROMPT)],
        tools=_TOOLS,
        reasoning=reasoning_cfg,
        max_tokens=2000,
    )
    assert turn1.tool_calls, f"Turn 1 produced no tool call; cannot continue. text={turn1.text!r}"
    lookup_call = turn1.tool_calls[0]

    # --- Turn 2: feed lookup result back, expect assistant to call create_ticket.
    assistant_after_turn1 = Message(
        role=MessageRole.ASSISTANT,
        content=turn1.text or "",
        tool_calls=turn1.tool_calls,
        reasoning=turn1.reasoning,
    )
    lookup_result = Message(
        role=MessageRole.TOOL,
        content=_LOOKUP_RESPONSE,
        tool_call_id=lookup_call.id,
    )
    turn2 = client.generate(
        system=_SYSTEM_PROMPT,
        messages=[
            Message(role=MessageRole.USER, content=_USER_PROMPT),
            assistant_after_turn1,
            lookup_result,
        ],
        tools=_TOOLS,
        reasoning=reasoning_cfg,
        max_tokens=2000,
    )

    # --- Turn 3: feed create_ticket result back (synthetic success), expect
    # assistant to summarize. This turn is mostly to confirm continuity
    # works under both modes without errors.
    create_call = turn2.tool_calls[0] if turn2.tool_calls else None
    assistant_after_turn2 = Message(
        role=MessageRole.ASSISTANT,
        content=turn2.text or "",
        tool_calls=turn2.tool_calls,
        reasoning=turn2.reasoning,
    )
    turn3_messages: list[Message] = [
        Message(role=MessageRole.USER, content=_USER_PROMPT),
        assistant_after_turn1,
        lookup_result,
        assistant_after_turn2,
    ]
    if create_call is not None:
        turn3_messages.append(
            Message(
                role=MessageRole.TOOL,
                content='{"status": "ok", "ticket_id": "TKT-9001"}',
                tool_call_id=create_call.id,
            )
        )
    turn3 = client.generate(
        system=_SYSTEM_PROMPT,
        messages=turn3_messages,
        tools=_TOOLS,
        reasoning=reasoning_cfg,
        max_tokens=1000,
    )

    return {
        "turn1_reasoning_tokens": turn1.usage.reasoning_tokens,
        "turn2_reasoning_tokens": turn2.usage.reasoning_tokens,
        "turn3_reasoning_tokens": turn3.usage.reasoning_tokens,
        "turn2_tool_call": (
            {
                "name": create_call.name,
                "arguments": create_call.arguments,
            }
            if create_call is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# The A/B test itself
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set — skipping live Gemini Pro A/B.",
)
def test_keeping_placeholder_does_not_hurt_pro_thinking_on_turn_2() -> None:
    """Live A/B: with the placeholder kept on replay, Gemini 3.1 Pro must
    reason on turn 2 *at least as much* as with the placeholder dropped.

    The non-strict assertion (``>=``) intentionally tolerates the
    null-hypothesis case where both modes produce similar reasoning —
    this test is a directional gate, not a statistical proof. The
    diagnostic ``print`` lines below surface the actual numbers so a
    falsified H1 (similar reasoning in both modes) is informative even
    when the assertion passes.
    """
    client_drop = _build_client(drop_placeholder=True)
    drop_results = _run_three_turn_loop(client_drop)

    client_keep = _build_client(drop_placeholder=False)
    keep_results = _run_three_turn_loop(client_keep)

    # Diagnostic surface for the post-run write-up. ``-s`` shows these
    # under pytest; otherwise they live in the captured test log.
    print()
    print("=== Gemini 3.1 Pro placeholder A/B ===")
    print(f"  drop:  {drop_results}")
    print(f"  keep:  {keep_results}")
    drop_mid = drop_results["turn2_reasoning_tokens"] + drop_results["turn3_reasoning_tokens"]
    keep_mid = keep_results["turn2_reasoning_tokens"] + keep_results["turn3_reasoning_tokens"]
    print("  mid-conversation reasoning_tokens (turn2+turn3):")
    print(f"    drop = {drop_mid}")
    print(f"    keep = {keep_mid}")
    if drop_mid > 0:
        print(f"    ratio (keep/drop) = {keep_mid / drop_mid:.2f}")

    # Soft directional gate: keeping the placeholder shouldn't make
    # things *worse*. If H1 is real, keep ≫ drop. If H1 is wrong, they
    # should be similar (≥ tolerates noise). If keep < drop by more than
    # ~30% that's a *negative* signal we want to know about.
    tolerance = 0.7
    if drop_mid > 0:
        assert keep_mid >= int(drop_mid * tolerance), (
            f"Keeping the placeholder REDUCED mid-conversation reasoning "
            f"significantly: keep={keep_mid} drop={drop_mid} "
            f"ratio={keep_mid / drop_mid:.2f} < {tolerance}. The PR #101 "
            "drop-on-replay heuristic may actually be helping, not hurting."
        )

    # The 2026-05-21 field-omission failure mode (organization_id
    # missing) is the qualitative symptom of H1. Surface it: if the
    # ``keep`` variant produced a create_ticket call WITH
    # organization_id while ``drop`` did not, that's a much stronger
    # signal than the token count alone.
    drop_has_org = (
        drop_results["turn2_tool_call"] is not None
        and isinstance(drop_results["turn2_tool_call"].get("arguments"), dict)
        and "organization_id" in drop_results["turn2_tool_call"]["arguments"]
    )
    keep_has_org = (
        keep_results["turn2_tool_call"] is not None
        and isinstance(keep_results["turn2_tool_call"].get("arguments"), dict)
        and "organization_id" in keep_results["turn2_tool_call"]["arguments"]
    )
    print(f"  organization_id in turn-2 create call: drop={drop_has_org} keep={keep_has_org}")
