"""Cleanup continues after failures and reports them to the trial owner."""

import pytest

from tolokaforge.runner.tool_factory import ReconstructedTools

pytestmark = pytest.mark.unit


def test_cleanup_attempts_every_resource_even_when_stop_fails():
    events = []

    class Resource:
        has_lifecycle = True

        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def stop(self):
            events.append((self.name, "stop"))
            if self.fail:
                raise RuntimeError("cannot stop resource")

        def cleanup(self):
            events.append((self.name, "cleanup"))

    bad, good = Resource("bad", True), Resource("good")
    owned = ReconstructedTools(agent_tools={"bad": bad, "good": good}, user_tools={"shared": good})
    with pytest.raises(RuntimeError, match="cannot stop resource"):
        owned.cleanup()
    assert events == [("bad", "stop"), ("bad", "cleanup"), ("good", "stop"), ("good", "cleanup")]
