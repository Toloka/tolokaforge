"""HTTP request tool for mock web services"""

from typing import Any
from urllib.parse import urlparse

import httpx

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class HTTPRequestTool(Tool):
    """Make HTTP requests to mock web services"""

    def __init__(self, allowed_hosts: list[str] | None = None):
        policy = ToolPolicy(
            timeout_s=20.0,
            category=ToolCategory.COMPUTE,
            visibility=["agent"],
        )
        super().__init__(
            name="http_request",
            description="Make HTTP requests to web services",
            policy=policy,
        )
        # Default allowed hosts for mock services
        self.allowed_hosts = allowed_hosts or [
            "mock-web",
            "mock-web:8080",
            "localhost:8080",
        ]

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
                            "enum": ["GET", "POST", "PUT", "DELETE"],
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

        # Scrub headers
        headers = self._scrub_headers(headers)

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
                output=output,
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
                error=f"HTTP request failed: {str(e)}",
            )
