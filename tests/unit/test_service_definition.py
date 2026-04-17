"""Unit tests for ServiceDefinition, ServiceStatus, and ServiceStack dependency ordering.

These tests validate the Pydantic models and topological sort logic
without requiring Docker.
"""

import pytest

from tolokaforge.docker.stack import ServiceDefinition, ServiceStack

pytestmark = pytest.mark.unit

# =============================================================================
# ServiceStack Dependency Ordering Tests
# =============================================================================


class TestServiceStackDependencyOrdering:
    """Tests for ServiceStack._topological_sort() — no Docker required."""

    @pytest.mark.unit
    def test_no_dependencies(self):
        """Services with no dependencies can start in any (deterministic) order."""
        services = {
            "a": ServiceDefinition(name="a", image_name="img-a"),
            "b": ServiceDefinition(name="b", image_name="img-b"),
            "c": ServiceDefinition(name="c", image_name="img-c"),
        }
        order = ServiceStack._topological_sort(services)
        assert set(order) == {"a", "b", "c"}
        assert order == ["a", "b", "c"]

    @pytest.mark.unit
    def test_linear_dependencies(self):
        """Linear chain: c -> b -> a."""
        services = {
            "a": ServiceDefinition(name="a", image_name="img-a"),
            "b": ServiceDefinition(name="b", image_name="img-b", depends_on=["a"]),
            "c": ServiceDefinition(name="c", image_name="img-c", depends_on=["b"]),
        }
        order = ServiceStack._topological_sort(services)
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    @pytest.mark.unit
    def test_diamond_dependencies(self):
        """Diamond: d -> {b, c} -> a."""
        services = {
            "a": ServiceDefinition(name="a", image_name="img"),
            "b": ServiceDefinition(name="b", image_name="img", depends_on=["a"]),
            "c": ServiceDefinition(name="c", image_name="img", depends_on=["a"]),
            "d": ServiceDefinition(name="d", image_name="img", depends_on=["b", "c"]),
        }
        order = ServiceStack._topological_sort(services)
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    @pytest.mark.unit
    def test_circular_dependency_raises(self):
        """Circular dependency raises ValueError."""
        services = {
            "a": ServiceDefinition(name="a", image_name="img", depends_on=["b"]),
            "b": ServiceDefinition(name="b", image_name="img", depends_on=["a"]),
        }
        with pytest.raises(ValueError, match="Circular dependency"):
            ServiceStack._topological_sort(services)

    @pytest.mark.unit
    def test_missing_dependency_raises(self):
        """Referencing non-existent service raises ValueError."""
        services = {
            "a": ServiceDefinition(name="a", image_name="img", depends_on=["nonexistent"]),
        }
        with pytest.raises(ValueError, match="not in the stack"):
            ServiceStack._topological_sort(services)


# =============================================================================
# ServiceStack Service Management Tests
# =============================================================================


class TestServiceStackManagement:
    """Tests for ServiceStack add/filter methods — no Docker required."""

    @pytest.mark.unit
    def test_add_service(self):
        """add_service() adds a service to the stack."""
        stack = ServiceStack()
        svc = ServiceDefinition(name="test", image_name="img")
        stack.add_service(svc)
        assert "test" in stack.services
        assert stack.services["test"] is svc

    @pytest.mark.unit
    def test_add_duplicate_raises(self):
        """add_service() raises on duplicate name."""
        stack = ServiceStack()
        svc = ServiceDefinition(name="test", image_name="img")
        stack.add_service(svc)
        with pytest.raises(ValueError, match="already exists"):
            stack.add_service(svc)

    @pytest.mark.unit
    def test_filter_by_profiles_match(self):
        """Profile filtering includes matching and no-profile services."""
        stack = ServiceStack()
        stack.add_service(ServiceDefinition(name="core-svc", image_name="img", profiles=["core"]))
        stack.add_service(ServiceDefinition(name="rag-svc", image_name="img", profiles=["rag"]))
        stack.add_service(ServiceDefinition(name="always", image_name="img"))

        result = stack._filter_by_profiles(["core"])
        assert "core-svc" in result
        assert "always" in result
        assert "rag-svc" not in result
