"""Integration tests for the :mod:`tolokaforge.core.llm` layer.

Every file in this package is :mod:`pytest.mark.integration`-gated and runs
manually / nightly only — **never in regular CI**. Per Stage 0's locked
design decision, live-provider calls never block PRs.

Run with::

    scripts/with_env.sh uv run pytest tests/integration/llm/ -v

Stage 8 will grow this directory into a per-capability suite
(:class:`ModelCertificate` registry). Stage 1 seeds it with a single test
for the Decimal-field tool-call P1 regression guard.
"""
