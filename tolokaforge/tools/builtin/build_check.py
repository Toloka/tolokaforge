"""``build_check`` builtin — quick compile / interface-compatibility probe.

An agent-facing tool that POSTs (or GETs) a task-declared HTTP endpoint on
a peer compose service and returns the response body as tool output. The
tool takes no per-invocation arguments — the endpoint is declared once
at task-authoring time via ``tool_config``, matching the reference-eval
shape of a ``tests_helper``-style probe without hard-coding any
adapter-specific concerns in core.

Config surface (routed via ``ToolSchema.tool_config`` under
``Dispatch.GENERIC``):

* ``service`` (str, required) — compose service name to probe.
* ``port`` (int, default 8001) — port on that service.
* ``path`` (str, default ``"/build_check"``) — endpoint path.
* ``method`` (``"GET"`` | ``"POST"``, default ``"POST"``) — HTTP verb.
* ``timeout_s`` (float, default 300.0) — request timeout.

Because the target is a peer service on the trial's compose network, the
request happens from the runner container (which resolves ``<service>``
via docker's built-in DNS) and stays entirely within the compose network
— no external network access required.

The tool returns the peer's response body verbatim; the peer is trusted
to bound its own response size. There is no tool-side cap.
"""

from typing import Any

import httpx

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class BuildCheckTool(Tool):
    """Compile / interface-compatibility probe against a peer service.

    Emits a single HTTP request to ``http://{service}:{port}{path}`` and
    returns the response body as tool output. The endpoint owns the
    response shape (adapter-specific); the tool passes it back verbatim.

    Failures surface as :class:`ToolResult` with ``success=False`` and
    ``error`` populated so the loop-side error surface treats them as
    tool errors, not infrastructure errors — the agent can retry or
    work around them without terminating the trial.
    """

    def __init__(
        self,
        service: str,
        port: int = 8001,
        path: str = "/build_check",
        method: str = "POST",
        timeout_s: float = 300.0,
    ) -> None:
        if not isinstance(service, str) or not service:
            raise ValueError(
                f"build_check: ``service`` is required and must be a non-empty string; "
                f"got {service!r}"
            )
        if not isinstance(port, int) or isinstance(port, bool):
            raise ValueError(f"build_check: ``port`` must be an int; got {type(port).__name__}")
        if not isinstance(path, str):
            raise ValueError(f"build_check: ``path`` must be a string; got {type(path).__name__}")
        if method not in ("GET", "POST"):
            raise ValueError(f"build_check: ``method`` must be GET or POST; got {method!r}")
        if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ValueError(f"build_check: ``timeout_s`` must be positive; got {timeout_s!r}")

        self._service = service
        self._port = port
        self._path = path if path.startswith("/") else f"/{path}"
        self._method = method
        self._timeout_s = float(timeout_s)

        policy = ToolPolicy(
            timeout_s=self._timeout_s + 30.0,
            category=ToolCategory.COMPUTE,
            visibility=["agent"],
        )
        super().__init__(
            name="build_check",
            description=(
                "Validate compatibility of your code with the hidden test framework. "
                "Runs a quick build / interface-collection check against the peer "
                "service without executing the full test suite, and returns the "
                "build output (compile status, stdout/stderr tail). Use this to "
                "iterate on structural / compile errors before the final test run — "
                "much cheaper than waiting for the full grade pass. Takes no "
                "arguments — the endpoint is pre-configured for this task."
            ),
            policy=policy,
        )

    @property
    def endpoint_url(self) -> str:
        """Return the fully-resolved endpoint URL. Test-side introspection."""
        return f"http://{self._service}:{self._port}{self._path}"

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        """Invoke the endpoint and return the response body as tool output.

        Ignores any inbound kwargs — the endpoint contract is fully
        declared in tool config, so the agent-side call carries no
        arguments. Ignored rather than rejected so a call site that
        misreads the schema still gets a useful response.
        """
        del kwargs  # endpoint contract is config-driven; per-invocation args ignored
        try:
            if self._method == "GET":
                response = httpx.get(self.endpoint_url, timeout=self._timeout_s)
            else:
                response = httpx.post(
                    self.endpoint_url,
                    headers={"Content-Type": "application/json"},
                    content=b"{}",
                    timeout=self._timeout_s,
                )
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"build_check: request to {self.endpoint_url} timed out after "
                    f"{self._timeout_s}s"
                ),
            )
        except httpx.HTTPError as exc:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"build_check: request to {self.endpoint_url} failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        # Return the response body verbatim. The peer service owns the
        # payload shape (adapter-specific); the tool doesn't re-interpret
        # it. Any non-2xx (including 3xx — httpx does not follow redirects
        # here, so a redirect from a misconfigured peer is a defect, not a
        # success) still hands the body back so the agent can read
        # diagnostic detail an ``error`` string would elide.
        body_text = response.text
        if response.status_code < 200 or response.status_code >= 300:
            return ToolResult(
                success=False,
                output=body_text,
                error=(f"build_check: {self.endpoint_url} returned HTTP {response.status_code}"),
                metadata={"status_code": response.status_code},
            )
        return ToolResult(
            success=True,
            output=body_text,
            metadata={"status_code": response.status_code},
        )
