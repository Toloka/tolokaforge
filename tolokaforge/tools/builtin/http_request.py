"""HTTP request tool for mock web services.

The caller's headers are dropped except ``Content-Type``, ``Accept`` and
``User-Agent``, so the agent can present no credential of its own. A credential
comes from the runtime only: an app world (ADR-0058) hands the tool its actor's
bearer token through :meth:`HTTPRequestTool.present_bearer`, and the tool sets
``Authorization: Bearer <token>`` on requests to the world's hosts alone. The
token is in no schema, argument or result.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("mock-web", "mock-web:8080", "localhost:8080")
"""The hosts the tool reaches when ``allowed_hosts`` is not configured."""

REDACTED_CREDENTIAL = "***REDACTED***"
"""What a presented token reads as if a response quotes it back."""


def host_listed(url: str, hosts: Collection[str]) -> bool:
    """Whether ``url``'s host, bare or with its explicit port, is one of ``hosts``."""
    parsed = urlparse(url)
    if parsed.hostname is None:
        return False
    if parsed.hostname in hosts:
        return True
    return parsed.port is not None and f"{parsed.hostname}:{parsed.port}" in hosts


@dataclass(frozen=True)
class _Bearer:
    """A runtime credential and the only hosts it is presented to."""

    token: str
    hosts: frozenset[str]


class HTTPRequestTool(Tool):
    """Make HTTP requests to mock web services"""

    def __init__(self, allowed_hosts: list[str] | None = None):
        policy = ToolPolicy(
            timeout_s=20.0,
            category=ToolCategory.COMPUTE,
        )
        super().__init__(
            name="http_request",
            description="Make HTTP requests to web services",
            policy=policy,
        )
        self.allowed_hosts = allowed_hosts or list(DEFAULT_ALLOWED_HOSTS)
        self._bearer: _Bearer | None = None

    def present_bearer(self, token: str, hosts: Collection[str]) -> None:
        """Set ``Authorization: Bearer <token>`` on every later request to ``hosts``.

        The runtime's call, once per tool: an app world mints the token for the
        actor this tool is built for (ADR-0058). Requests to any other host carry
        no credential.

        Raises:
            ValueError: the token is empty, a host is one the tool never requests,
                or a credential was already presented.
        """
        if not token:
            raise ValueError("http_request: an empty bearer token authenticates nothing")
        if self._bearer is not None:
            raise ValueError(
                "http_request: a bearer token was already presented; one tool serves one "
                "actor, which holds one token"
            )
        unreachable = sorted(
            host
            for host in hosts
            if host not in self.allowed_hosts and host.split(":")[0] not in self.allowed_hosts
        )
        if unreachable:
            raise ValueError(
                f"http_request: the credential's hosts {unreachable!r} are not in "
                f"allowed_hosts {self.allowed_hosts!r}, so it would never be presented"
            )
        self._bearer = _Bearer(token=token, hosts=frozenset(hosts))

    def _credential_headers(self, url: str) -> dict[str, str]:
        if self._bearer is None or not host_listed(url, self._bearer.hosts):
            return {}
        return {"Authorization": f"Bearer {self._bearer.token}"}

    def _without_credential(self, text: str) -> str:
        """``text`` with the presented token masked, should a service quote it back."""
        if self._bearer is None:
            return text
        return text.replace(self._bearer.token, REDACTED_CREDENTIAL)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                            "description": "HTTP method",
                        },
                        "url": {
                            "type": "string",
                            "description": "URL to request (must be to allowed hosts)",
                        },
                        "headers": {
                            "type": "object",
                            "description": "Optional HTTP headers",
                        },
                        "json": {
                            "type": "object",
                            "description": "Optional JSON body for POST/PUT",
                        },
                        "data": {
                            "type": "object",
                            "description": "Optional form data for POST/PUT",
                        },
                    },
                    "required": ["method", "url"],
                    "additionalProperties": False,
                },
            },
        }

    def _is_allowed_host(self, url: str) -> bool:
        """Check if URL is to an allowed host"""
        parsed = urlparse(url)
        host_with_port = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname

        return (
            parsed.hostname in self.allowed_hosts
            or host_with_port in self.allowed_hosts
            or url.startswith("http://mock-web")
            or url.startswith("http://localhost:8080")
        )

    def _scrub_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        """Remove sensitive headers"""
        if not headers:
            return {}

        scrubbed = {}
        allowed_headers = [
            "content-type",
            "accept",
            "user-agent",
        ]

        for key, value in headers.items():
            if key.lower() in allowed_headers:
                scrubbed[key] = value

        return scrubbed

    def execute(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Execute HTTP request"""
        # Validate URL
        if not self._is_allowed_host(url):
            return ToolResult(
                success=False,
                output="",
                error=f"URL not allowed: {url}. Only mock services are accessible.",
            )

        # Drop the caller's headers, then attach the runtime's credential for this host.
        headers = {**self._scrub_headers(headers), **self._credential_headers(url)}

        try:
            # Make request
            response = httpx.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                data=data,
                timeout=self.policy.timeout_s,
                follow_redirects=True,
            )

            # Format response
            output = f"Status: {response.status_code}\n"

            if response.headers.get("content-type", "").startswith("application/json"):
                output += f"Response (JSON):\n{response.json()}"
            elif response.headers.get("content-type", "").startswith("text/html"):
                # For HTML, extract text content
                text = response.text[:2000]  # Limit HTML length
                output += f"Response (HTML snippet):\n{text}"
            else:
                output += f"Response:\n{response.text[:1000]}"

            return ToolResult(
                success=response.is_success,
                output=self._without_credential(output),
                metadata={
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type"),
                },
            )

        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                output="",
                error=f"Request timed out after {self.policy.timeout_s}s",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=self._without_credential(f"HTTP request failed: {str(e)}"),
            )
