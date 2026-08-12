"""Provider-scoped live integration tests for the :mod:`tolokaforge.core.llm` layer.

Every file here is :mod:`pytest.mark.integration`-gated and runs manually /
nightly only — never in regular CI. Live-provider calls do not block PRs.

This directory hosts the tests that live *outside* the capability-driven
certification suite:

* :mod:`test_nova_api` — provider-scoped Amazon Nova probes; not
  parametrised over :data:`tolokaforge.testing.certify.ALL_MODELS`.
* :mod:`test_gemini_placeholder_signature_replay` — one-off Gemini
  placeholder-block A/B experiment.
* :mod:`test_gateway_live` — live LLM gateway transport check, isolated
  from the certification credential set.

The capability-driven suite parametrised over
:data:`tolokaforge.testing.certify.ALL_MODELS` lives at
:mod:`tolokaforge.testing.certify.suite`.
"""
