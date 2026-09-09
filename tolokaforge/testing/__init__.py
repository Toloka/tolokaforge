"""Public engine-side testing seam.

Two subpackages ship the reusable pytest suites external callers subclass:

- :mod:`tolokaforge.testing.certify` — per-model capability suite. External
  callers drive it against their own model list by importing the certificate
  data types and probe-registration API here.
- :mod:`tolokaforge.testing.adapters` — adapter-contract suites. External
  adapter authors subclass one class per seam
  (:class:`~tolokaforge.testing.adapters.AdapterGradingContractSuite` today)
  to pin their adapter against the engine's declared shape.

This top-level package is a namespace marker only; consumers import from the
subpackage directly.
"""
