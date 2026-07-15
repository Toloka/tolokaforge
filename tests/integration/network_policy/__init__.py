"""Integration proof that ``manifest.network_policy`` enforces real egress.

Each module materialises a minimal two-service stack (an ``nginx`` runner
plus a ``curl`` application service) through the real
:class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend` and
asserts observable network behaviour against a live Docker daemon:

* ``test_no_internet`` — application egress (raw IP and DNS) is blocked
  while inter-service DNS and the host-published runner port stay reachable.
* ``test_full_internet`` — application egress succeeds (over-enforcement
  guard).
* ``test_limited_internet`` — materialisation refuses before any container
  starts.

Marked ``integration`` + ``docker`` — requires a real Docker daemon.
"""
