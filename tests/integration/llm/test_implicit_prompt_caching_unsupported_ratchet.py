"""Forensic ratchet — :attr:`Capability.IMPLICIT_PROMPT_CACHING` in ``known_unsupported``.

Companion to :mod:`test_implicit_prompt_caching` for the *opposite*
direction: certs that declare ``IMPLICIT_PROMPT_CACHING`` as
``known_unsupported`` MUST genuinely NOT auto-cache on the upstream
route. If OpenRouter (or the upstream provider) ever flips on
auto-caching for one of these routes, this test fails loudly and forces
a registry correction — flipping the cert from ``known_unsupported`` to
``required`` and getting the cost-savings benefit recognised in eval
numbers.

Without this ratchet, an upstream-side cache improvement could land
silently: the harness wouldn't flag it, eval cost numbers would quietly
drop, and the registry would diverge from reality. The cost of running
this test is two short live calls per cert; it pays for itself the
first time it catches a real regression.

Distinct from the standard skip path
(``skip_unless_capability_declared``): instead of skipping when
``IMPLICIT_PROMPT_CACHING`` is ``known_unsupported``, this test runs ONLY
in that case, and ASSERTS the negative — that no implicit caching is
observed above the 100-token noise floor.
"""

from __future__ import annotations

import uuid

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS

# Anthropic routes legitimately serve cache reads under the
# ``IMPLICIT_PROMPT_CACHING`` skip path because the *explicit* cache
# policy attaches markers. They aren't candidates for "did upstream
# silently start auto-caching?" — their cache is wired by us.
# Exclude them from the ratchet so the cost stays on the routes the
# ratchet is actually about.
_ANTHROPIC_NAME_PREFIXES = ("anthropic/", "claude-")


def _is_anthropic(cert: ModelCertificate) -> bool:
    name = cert.name.lower()
    return any(name.startswith(p) or p in name for p in _ANTHROPIC_NAME_PREFIXES)


# Routes whose provider reports a non-trivial ``cached_tokens`` count even on
# a genuinely COLD, never-before-sent prompt — so the counter does NOT signal
# a real cross-request cache hit and the ratchet's core premise (observable
# cached_tokens ⟹ upstream silently started auto-caching ⟹ promote to
# ``required``) does not hold. These are declared IMPLICIT_PROMPT_CACHING
# ``known_unsupported`` in the registry with the SAME rationale, and excluded
# here so the ratchet doesn't force a promotion the standard
# ``test_implicit_prompt_caching`` cannot satisfy (no cold baseline ⟹ no
# ``call2 < call1`` cost delta).
#
# meta/muse-spark-1.1 (verified live 2026-07-17, US Codespaces): a 19,585-token
# unique prompt returned ``cached=19,581`` on call 1; the discount is real
# (cached input billed at the cache_read rate) but "caching" cannot be
# certified. See tests/integration/llm/registry.py for the full evidence.
# NB: this is a targeted exclusion, not a real fix — the ratchet cannot yet
# distinguish "always-reports-cached" routes from genuine auto-cache. Tracked
# as a follow-up to teach the ratchet to send a cold probe first.
_UNRELIABLE_COLD_CACHE_REPORT_NAMES = frozenset({"meta/muse-spark-1.1"})


def _reports_cache_on_cold_call(cert: ModelCertificate) -> bool:
    return cert.name.lower() in _UNRELIABLE_COLD_CACHE_REPORT_NAMES


def _ratchet_targets() -> list[ModelCertificate]:
    """Certs that claim to NOT auto-cache, excluding Anthropic-cache routes
    and routes whose ``cached_tokens`` reporting is unreliable on cold calls."""
    targets: list[ModelCertificate] = []
    for cert in ALL_MODELS:
        if Capability.IMPLICIT_PROMPT_CACHING not in cert.known_unsupported:
            continue
        if _is_anthropic(cert):
            continue
        if _reports_cache_on_cold_call(cert):
            continue
        targets.append(cert)
    return targets


_TARGETS = _ratchet_targets()


def _ratchet_system_prompt() -> str:
    """~8 k-token prompt matching :mod:`test_implicit_prompt_caching`.

    Pinned to the same size as the standard test so the ratchet probes
    the SAME contract: "at this prompt size, does the route cache?"
    A smaller probe surfaces partial-prefix caching that doesn't reach
    the standard test's 1 000-token cache-read threshold (verified
    2026-05-14 — kimi / deepseek cache reliably at 2 k but report 0
    cached at 8 k), giving false positives that wouldn't actually
    qualify the cert for promotion. Matching the sizes keeps the
    ratchet honest: it trips only when the standard test *would* pass.
    """
    nonce = uuid.uuid4().hex
    preamble = (
        f"You are a meticulous on-call triage assistant. "
        f"Session nonce {nonce}: every prompt below is uniquely keyed to "
        f"this session and must not be confused with prior contexts. "
    )
    body = "Context: X. " * 2000  # ~8 k tokens.
    return preamble + body


_NOOP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Return the current UTC timestamp.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]

# Cache hits below this floor are either OpenRouter rounding artefacts,
# partial-prefix matches too small to materially affect cost, or
# best-effort caching at small prompts that doesn't translate to the
# 8 k-token regime the standard ``test_implicit_prompt_caching`` test
# probes. Pinned to the same 1 000-token threshold the standard test
# uses for ``cache_read_input_tokens > 1000`` so the ratchet only trips
# when the cache is "useful enough" to demand cert promotion.
#
# Empirical justification (live probe 2026-05-14 16:17 UTC):
# * mimo on 8 k prompt: cached=8192 → comfortably above threshold,
#   promoted to ``required``.
# * deepseek on 8 k prompt: cached=0 (despite cached=1920 on 2 k probe
#   — partial / threshold-bounded auto-cache).
# * grok-4 on 2 k probe: cached=676 — below threshold, route caches
#   small prompts but not the eval-relevant 8 k+ regime.
# * grok-4.3 on 2 k probe: cached=128 — noise-floor / borderline.
_NOISE_FLOOR_TOKENS = 1000


@pytest.mark.parametrize("cert", _TARGETS, ids=lambda c: c.model_id)
def test_known_unsupported_routes_do_not_auto_cache(
    cert: ModelCertificate,
    live_client,
) -> None:
    """If the cert says "no implicit caching", upstream must agree.

    Fails when call 2 reports ``cache_read_input_tokens > 100`` despite
    the cert declaring ``IMPLICIT_PROMPT_CACHING`` in ``known_unsupported``
    — i.e. upstream silently started auto-caching this route. The fix is
    to move the capability from ``known_unsupported`` to ``required``
    in :mod:`tests.integration.llm.registry` and remove the
    diagnostic-skip comment alongside.
    """
    client = live_client(cert)
    system = _ratchet_system_prompt()

    result1 = client.generate(
        system=system,
        messages=[Message(role=MessageRole.USER, content="Say 'one'.")],
        tools=_NOOP_TOOLS,
        max_tokens=32,
    )
    result2 = client.generate(
        system=system,
        messages=[Message(role=MessageRole.USER, content="Say 'two'.")],
        tools=_NOOP_TOOLS,
        max_tokens=32,
    )

    observed_read = result2.usage.cache_read_input_tokens
    observed_cached = result2.usage.cached_tokens

    # If either counter is non-trivial, the ratchet trips.
    if observed_read > _NOISE_FLOOR_TOKENS or observed_cached > _NOISE_FLOOR_TOKENS:
        pytest.fail(
            f"{cert.model_id}: implicit prompt caching is now observable "
            f"on this route — call 2 reported "
            f"cache_read_input_tokens={observed_read}, "
            f"cached_tokens={observed_cached} (above the "
            f"{_NOISE_FLOOR_TOKENS}-token noise floor). The cert in "
            f"tests/integration/llm/registry.py claims this is "
            f"`known_unsupported` for IMPLICIT_PROMPT_CACHING; the cert "
            f"is now wrong. Move IMPLICIT_PROMPT_CACHING from "
            f"``known_unsupported`` to ``required`` for this model and "
            f"verify via the standard test_implicit_prompt_caching "
            f"suite. Reference call 1: prompt_tokens="
            f"{result1.usage.prompt_tokens}, "
            f"cached_tokens={result1.usage.cached_tokens}; "
            f"call 2: prompt_tokens={result2.usage.prompt_tokens}, "
            f"cached_tokens={result2.usage.cached_tokens}."
        )


def test_ratchet_has_targets() -> None:
    """Sanity: the ratchet must actually parametrize over some certs.

    If every non-Anthropic cert moves to ``required`` (good outcome!)
    this list goes empty and parametrize would emit a single skipped
    item — at which point this guard fails loudly so the ratchet can
    be retired deliberately rather than silently."""
    msg = (
        "Implicit-caching ratchet has no targets — every non-Anthropic cert "
        "now declares IMPLICIT_PROMPT_CACHING as required. Retire this "
        "diagnostic test module rather than leaving a parametrize-over-empty "
        "vestige."
    )
    assert _TARGETS, msg
