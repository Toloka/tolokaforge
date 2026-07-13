"""Small generic ACP client used inside BYOH agent containers.

This intentionally implements the protocol surface needed for headless benchmark
agents and the local contract mock. Terminal delegation remains disabled: agents
should operate in their own container and use the Runner's MCP tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.interfaces import Client
from acp.schema import (
    AllowedOutcome,
    AuthCapabilities,
    ClientCapabilities,
    FileSystemCapabilities,
    HttpHeader,
    HttpMcpServer,
    ReadTextFileResponse,
    RequestPermissionResponse,
    WriteTextFileResponse,
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


class TolokaforgeACPClient(Client):
    def on_connect(self, conn: Any) -> None:
        self._record("connect", {"connection": type(conn).__name__})

    def _record(self, event_type: str, payload: Any) -> None:
        print(
            json.dumps(
                {"event_type": event_type, "payload": _jsonable(payload)},
                ensure_ascii=False,
            ),
            flush=True,
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._record("session_update", {"session_id": session_id, "update": update})

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any
    ) -> RequestPermissionResponse:
        if not options:
            raise RuntimeError("ACP agent requested permission without any options")
        option_id = options[0].option_id
        self._record("permission", {"session_id": session_id, "option_id": option_id})
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        resolved = Path(path).resolve()
        resolved.relative_to(Path("/work"))
        lines = resolved.read_text(encoding="utf-8").splitlines(keepends=True)
        start = max((line or 1) - 1, 0)
        selected = lines[start : start + limit if limit is not None else None]
        return ReadTextFileResponse(content="".join(selected))

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> WriteTextFileResponse:
        resolved = Path(path).resolve()
        resolved.relative_to(Path("/work"))
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._record("extension_method", {"method": method, "params": params})
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self._record("extension_notification", {"method": method, "params": params})


async def _run(config: dict[str, Any]) -> int:
    command = config["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        raise ValueError("--command-json must contain a non-empty string array")
    headers = [HttpHeader(name="Authorization", value=f"Bearer {config['mcp_bearer_token']}")]
    mcp_servers = [
        HttpMcpServer(type="http", name="tolokaforge", url=config["mcp_url"], headers=headers)
    ]
    client = TolokaforgeACPClient()
    async with spawn_agent_process(
        client,
        command[0],
        *command[1:],
        cwd=config["cwd"],
        transport_kwargs={"stderr": None},
    ) as (connection, _process):
        initialized = await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                auth=AuthCapabilities(terminal=False),
                fs=FileSystemCapabilities(readTextFile=True, writeTextFile=True),
                terminal=False,
            ),
        )
        client._record("initialize", initialized)
        session = await connection.new_session(cwd=config["cwd"], mcp_servers=mcp_servers)
        client._record("new_session", session)
        response = await connection.prompt(
            session_id=session.session_id,
            prompt=[text_block(config["instruction"])],
        )
        client._record("prompt_response", response)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    return asyncio.run(_run(config))


if __name__ == "__main__":
    raise SystemExit(main())
