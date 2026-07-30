"""Deferred host-port placeholders in health-probe URLs.

Services created with auto-allocated host ports cannot know the port at
definition time. ``{port:<container_port>}`` placeholders let them keep a
rich probe (custom timeout/path) instead of losing it to the generic
port-8000 fallback — e.g. rag-service's long cold-start timeout, which a
30s default probe would break on first-run model downloads.
"""

import pytest

from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.stack import EngineStack

pytestmark = pytest.mark.unit


def test_placeholder_resolved_from_port_map():
    probe = HealthProbe.http(url="http://localhost:{port:8001}/health", timeout_s=300.0)
    resolved = EngineStack._resolve_health_probe(probe, {8001: 45678})
    assert resolved is not None
    assert resolved.url == "http://localhost:45678/health"
    assert resolved.timeout_s == 300.0  # custom timeout survives resolution


def test_placeholder_with_unresolved_port_raises():
    # Every service declaring a placeholder also declares the matching
    # PortConfig, so an unresolvable placeholder is a programming error —
    # dropping the probe here would silently skip the health wait.
    probe = HealthProbe.http(url="http://localhost:{port:8001}/health")
    with pytest.raises(ValueError, match="no resolved host mapping"):
        EngineStack._resolve_health_probe(probe, {8000: 45678})


def test_concrete_url_returned_unchanged():
    probe = HealthProbe.http(url="http://localhost:8001/health", timeout_s=120.0)
    resolved = EngineStack._resolve_health_probe(probe, {8001: 45678})
    assert resolved is probe


def test_default_probe_still_built_for_port_8000():
    resolved = EngineStack._resolve_health_probe(None, {8000: 39999})
    assert resolved is not None
    assert resolved.url == "http://localhost:39999/health"
