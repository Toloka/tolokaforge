"""Leaf module for network-policy substrate constants.

Both :mod:`tolokaforge.runner.models` (manifest validators) and
:mod:`tolokaforge.core.compose_materialisation` (compose-doc transform)
import from here. No behavioural code; no cross-module imports beyond
stdlib.
"""

from __future__ import annotations

NETPOLICY_INTERNAL_NETWORK = "tolokaforge_netpolicy_internal"
"""Injected ``internal: true`` network every task service joins under
``no_internet``. No public egress; inter-service DNS stays intact because
every service shares it. Compose prefixes it with the per-run/per-trial
project name, so the fully-qualified network is unique on the daemon and
cannot collide with a task-declared network of the same base name."""

NETPOLICY_EDGE_NETWORK = "tolokaforge_netpolicy_edge"
"""Injected non-internal network the runner service *additionally* joins
under ``no_internet``, so its published gRPC port stays host-reachable and
it retains egress for in-container LLM-as-judge grading."""

NETPOLICY_PROXY_SERVICE = "tolokaforge_netpolicy_proxy"
"""Injected forward-proxy sidecar under ``limited_internet``. Sits on both
the internal and edge networks; every application service's HTTP(S) egress is
routed through it, and it forwards only to the declared allowlist."""

NETPOLICY_PROXY_PORT = 3128
"""Port the injected squid proxy listens on. Application services'
``HTTP(S)_PROXY`` env vars point here."""

NETPOLICY_PROXY_IMAGE = (
    "ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029"
)
"""Digest-pinned squid image (Canonical ``ubuntu/squid:6.6-24.04_beta``).
Pinned by digest, not tag, because Canonical's tag channels are mutable."""

HARNESS_RESERVED_NETWORKS: frozenset[str] = frozenset(
    {NETPOLICY_INTERNAL_NETWORK, NETPOLICY_EDGE_NETWORK}
)
"""Injected network names owned by the network-policy transform. A
restricted service that pre-declares one of these in its compose
``networks:`` block would silently defeat the partitioning primitive,
since the transform skips restricted services and leaves their
``networks:`` verbatim."""
