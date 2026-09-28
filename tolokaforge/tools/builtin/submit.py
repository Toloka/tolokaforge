"""The agent's completion tool: an explicit end-of-episode signal.

Without it the only clean exit from the agent's turn loop is a turn that
carries no tool call — a shape the loop reads as "nothing left to do" and
routes to the turn policy. That makes termination a property of how a model
writes rather than of what it decides: a model that narrates and signs off
lands it, a model that answers every turn with a bare tool call never does.

``submit`` closes that gap the way the rubric judge's ``submit_report``
already does on the grading side: the terminal act is a tool call, so any
model that can call a tool can end its own episode. The termination itself
is a :class:`~tolokaforge.core.loop.TerminationPolicy` decision made by the
trial runner — the loop consults the policy before it executes anything, so
this tool's :meth:`SubmitTool.execute` is not on the path a terminating call
takes. It exists because a builtin is a tool like any other: the runner
reconstructs it at trial registration, and a tool that could not answer if
it were called would be a lie about what is registered.

Opt-in. The name appears in ``tools.agent.enabled`` only where an operator
puts it, so every pack that terminates through its user simulator is
untouched.
"""

from typing import Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

SUBMIT_TOOL_NAME = "submit"


class SubmitTool(Tool):
    """Declare the assigned work finished and end the episode.

    The optional ``summary`` is the agent's own account of what it did. It is
    optional on purpose: requiring an argument makes the signal cost a
    sentence, and the measurement this tool exists for is whether a model
    reaches for the signal at all.
    """

    def __init__(self) -> None:
        super().__init__(
            name=SUBMIT_TOOL_NAME,
            description=(
                "Call this when you have finished the assigned work and have nothing "
                "further to do. It ends the episode immediately: no later turn runs, "
                "and no tool call you make alongside it is executed. Verify your work "
                "first — once this is called you cannot act again."
            ),
            policy=ToolPolicy(timeout_s=5.0, category=ToolCategory.COMPUTE),
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": (
                                "Optional one-paragraph account of what you did and why "
                                "the work is complete."
                            ),
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, summary: str = "") -> ToolResult:
        """Acknowledge the signal.

        Reached only when the tool is called somewhere the runner's completion
        policy does not govern the loop — the ``submit`` name is enabled but
        the caller drives its own turn cycle. Nothing here ends anything; the
        output says only that the signal was received, so a caller reading the
        transcript cannot mistake it for a verdict on the work.
        """
        return ToolResult(
            success=True,
            output="Completion signal recorded.",
            metadata={"summary": summary},
        )
