"""Containerized BYOH trial execution and Claude stream-to-ATIF conversion."""

from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import (
    AgentHarnessConfig,
    Message,
    MessageRole,
    Metrics,
    ToolCall,
    Trajectory,
)
from tolokaforge.docker.container import Container, ExecResult
from tolokaforge.docker.image import Image
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.network import Network
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.harnesses.container_environment import AgentContainerEnvironment
from tolokaforge.harnesses.registry import get_harness_spec
from tolokaforge.harnesses.vendor.harbor.base import classify_agent_exit
from tolokaforge.harnesses.vendor.harbor.claude_code import build_claude_command
from tolokaforge.harnesses.vendor.harbor.trajectory import messages_to_atif
from tolokaforge.secrets import get_default


@dataclass
class HarnessRunResult:
    trajectory: Trajectory
    raw_stdout: str
    raw_stderr: str
    atif: dict[str, Any]
    workspace: Path


class HarnessTrialRunner:
    """Run one configured harness in an isolated per-trial container."""

    def __init__(
        self,
        config: AgentHarnessConfig,
        *,
        network: Network,
        workspace_root: Path,
        episode_timeout_s: float,
        proxy_url: str | None = None,
    ) -> None:
        self.config = config
        self.spec = get_harness_spec(config.type)
        self.network = network
        self.workspace_root = workspace_root.resolve()
        self.episode_timeout_s = episode_timeout_s
        self.proxy_url = proxy_url

    def run(
        self,
        *,
        task_id: str,
        trial_index: int,
        instruction: str,
        system_prompt: str,
        mcp_url: str,
        mcp_bearer_token: str,
        workspace: Path | None = None,
    ) -> HarnessRunResult:
        if self.config.type not in {"claude-code", "codex", "acp"}:
            raise NotImplementedError(
                f"Harness runtime {self.config.type!r} is registered but not implemented yet"
            )

        started = datetime.now(timezone.utc)
        workspace = workspace or self.prepare_workspace(task_id, trial_index)
        workspace = workspace.resolve()
        try:
            workspace.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"Harness workspace must be within {self.workspace_root}: {workspace}"
            ) from exc
        container = self._create_container(task_id, trial_index, workspace)
        environment = AgentContainerEnvironment(container)
        raw_stdout = ""
        raw_stderr = ""
        timed_out = False
        try:
            container.start()
            if self.config.type == "claude-code":
                mcp_config = {
                    "mcpServers": {
                        "tolokaforge": {
                            "type": "http",
                            "url": mcp_url,
                            "headers": {"Authorization": f"Bearer {mcp_bearer_token}"},
                        }
                    }
                }
                container.write_file(
                    "/tmp/tolokaforge-mcp.json",
                    json.dumps(mcp_config, separators=(",", ":")).encode("utf-8"),
                )
                command = self._claude_command(instruction, system_prompt)
            elif self.config.type == "codex":
                config_toml = (
                    "[mcp_servers.tolokaforge]\n"
                    f"url = {json.dumps(mcp_url)}\n"
                    f"http_headers = {{ Authorization = {json.dumps(f'Bearer {mcp_bearer_token}')} }}\n"
                    "required = true\n"
                )
                container.write_file("/tmp/codex-home/config.toml", config_toml.encode("utf-8"))
                command = self._codex_command(instruction, system_prompt)
            else:
                acp_config = {
                    "command": self.config.flags["command"],
                    "cwd": self.config.flags.get("cwd", "/work"),
                    "instruction": (
                        f"{system_prompt}\n\n{instruction}" if system_prompt else instruction
                    ),
                    "mcp_url": mcp_url,
                    "mcp_bearer_token": mcp_bearer_token,
                }
                container.write_file(
                    "/tmp/tolokaforge-acp.json",
                    json.dumps(acp_config, separators=(",", ":")).encode("utf-8"),
                )
                command = [
                    "/opt/acp/bin/python",
                    "/opt/tolokaforge-acp-runner.py",
                    "--config",
                    "/tmp/tolokaforge-acp.json",
                ]
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="byoh-agent")
            future = executor.submit(environment.exec, command)
            try:
                result = future.result(timeout=self.episode_timeout_s)
            except FutureTimeoutError:
                timed_out = True
                container.destroy()
                result = ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=f"Harness timed out after {self.episode_timeout_s}s",
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            raw_stdout = result.stdout
            raw_stderr = result.stderr
        finally:
            if container.current_status.value != "destroyed":
                container.destroy()

        converter = {
            "claude-code": self._convert_claude_stream,
            "codex": self._convert_codex_stream,
            "acp": self._convert_acp_stream,
        }[self.config.type]
        trajectory, atif = converter(
            task_id=task_id,
            trial_index=trial_index,
            instruction=instruction,
            stdout=raw_stdout,
            stderr=raw_stderr,
            exit_code=result.exit_code,
            started=started,
            timed_out=timed_out,
        )
        return HarnessRunResult(
            trajectory=trajectory,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            atif=atif,
            workspace=workspace,
        )

    def prepare_workspace(self, task_id: str, trial_index: int) -> Path:
        """Create the host directory shared by the Runner and agent container."""
        safe_task = re.sub(r"[^a-zA-Z0-9_.-]", "_", task_id)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = self.workspace_root / safe_task / str(trial_index) / uuid.uuid4().hex
        workspace.mkdir(parents=True, exist_ok=False)
        return workspace.resolve()

    def _create_container(self, task_id: str, trial_index: int, workspace: Path) -> Container:
        repo_root = Path(__file__).resolve().parents[2]
        image = Image.build(
            dockerfile=str(repo_root / "tolokaforge/docker/dockerfiles/agent_harness.Dockerfile"),
            context=str(repo_root),
            build_args={
                "HARNESS_TYPE": self.config.type,
                "HARNESS_VERSION": self.config.version,
            },
            name="tolokaforge-agent-harness",
        )
        safe_task = re.sub(r"[^a-zA-Z0-9_.-]", "_", task_id)[:80]
        secret_manager = get_default()
        environment = dict(self.config.env)
        if self.proxy_url:
            environment.update(
                {
                    "HTTP_PROXY": self.proxy_url,
                    "HTTPS_PROXY": self.proxy_url,
                    "http_proxy": self.proxy_url,
                    "https_proxy": self.proxy_url,
                    "NO_PROXY": "runner,localhost,127.0.0.1",
                    "no_proxy": "runner,localhost,127.0.0.1",
                }
            )
        secret_keys = list(self.spec.required_secret_keys)
        if self.config.type == "codex":
            # Current Codex automation accepts CODEX_API_KEY for one exec.
            # Keep OPENAI_API_KEY as the public Tolokaforge credential contract,
            # resolving the alias through SecretManager rather than os.environ.
            secret_manager.validate_required(secret_keys)
            environment["CODEX_API_KEY"] = secret_manager.get_secret("OPENAI_API_KEY") or ""
            secret_keys = []
            environment["CODEX_HOME"] = "/tmp/codex-home"

        return Container.create(
            image=image,
            name=f"tolokaforge-agent-{safe_task}-{trial_index}",
            mounts=[Mount.bind(str(workspace), "/work")],
            network=self.network,
            resources=ResourcePolicy(
                cpu_limit=2.0,
                memory_limit="4g",
                cap_drop=[Capability.ALL],
                no_new_privileges=True,
            ),
            environment=environment,
            command=["sleep", "infinity"],
            secret_keys=secret_keys,
            secret_manager=secret_manager,
        )

    def _claude_command(self, instruction: str, system_prompt: str) -> list[str]:
        return build_claude_command(
            instruction,
            system_prompt,
            self.config.flags,
            mcp_config_path="/tmp/tolokaforge-mcp.json",
        )

    def _codex_command(self, instruction: str, system_prompt: str) -> list[str]:
        flags = self.config.flags
        prompt = f"{system_prompt}\n\n{instruction}" if system_prompt else instruction
        command = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--sandbox",
            str(flags.get("sandbox_mode", "workspace-write")),
            "-c",
            f"approval_policy={json.dumps(flags.get('approval_policy', 'never'))}",
            "-C",
            "/work",
        ]
        if flags.get("model"):
            command.extend(["--model", str(flags["model"])])
        command.append(prompt)
        return command

    def _convert_claude_stream(
        self,
        *,
        task_id: str,
        trial_index: int,
        instruction: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        started: datetime,
        timed_out: bool,
    ) -> tuple[Trajectory, dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)

        messages = [Message(role=MessageRole.USER, content=instruction)]
        session_id = None
        final_event: dict[str, Any] = {}
        for event in events:
            session_id = session_id or event.get("session_id") or event.get("sessionId")
            event_type = event.get("type")
            if event_type == "assistant":
                message = event.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else []
                text_parts: list[str] = []
                tool_calls: list[ToolCall] = []
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            ToolCall(
                                id=str(block.get("id", "")),
                                name=str(block.get("name", "")),
                                arguments=block.get("input", {}),
                            )
                        )
                if text_parts or tool_calls:
                    messages.append(
                        Message(
                            role=MessageRole.ASSISTANT,
                            content="\n\n".join(text_parts),
                            tool_calls=tool_calls or None,
                        )
                    )
            elif event_type == "user":
                message = event.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else []
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        value = block.get("content", "")
                        if not isinstance(value, str):
                            value = json.dumps(value, default=str)
                        messages.append(
                            Message(
                                role=MessageRole.TOOL,
                                content=value,
                                tool_call_id=str(block.get("tool_use_id", "")),
                            )
                        )
            elif event_type == "result":
                final_event = event

        classified = classify_agent_exit(
            exit_code=(1 if final_event.get("is_error", False) else exit_code),
            output=f"{stderr}\n{final_event.get('result', '')}",
            timed_out=timed_out,
        )

        usage_data = final_event.get("usage", {})
        usage = Usage(
            prompt_tokens=int(usage_data.get("input_tokens", 0) or 0),
            completion_tokens=int(usage_data.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage_data.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(usage_data.get("cache_read_input_tokens", 0) or 0),
        )
        trajectory = Trajectory(
            task_id=task_id,
            trial_index=trial_index,
            start_ts=started,
            end_ts=datetime.now(timezone.utc),
            status=classified.status,
            termination_reason=classified.reason,
            messages=messages,
            metrics=Metrics(
                latency_total_s=(datetime.now(timezone.utc) - started).total_seconds(),
                turns=int(final_event.get("num_turns", 0) or 0),
                api_calls=int(final_event.get("num_turns", 0) or 0),
                usage=usage,
                cost_usd=float(final_event.get("total_cost_usd", 0.0) or 0.0),
                tool_calls=sum(len(message.tool_calls or []) for message in messages),
            ),
        )
        return trajectory, messages_to_atif(
            messages,
            session_id=str(session_id or f"{task_id}:{trial_index}"),
            harness_type=self.config.type,
            harness_version=self.config.version,
            model_name=self.config.flags.get("model"),
            final_event=final_event,
        )

    def _convert_codex_stream(
        self,
        *,
        task_id: str,
        trial_index: int,
        instruction: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        started: datetime,
        timed_out: bool,
    ) -> tuple[Trajectory, dict[str, Any]]:
        """Import the documented ``codex exec --json`` JSONL event stream."""

        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)

        messages = [Message(role=MessageRole.USER, content=instruction)]
        session_id = f"{task_id}:{trial_index}"
        usage_data: dict[str, Any] = {}
        event_errors: list[str] = []
        for event in events:
            event_type = event.get("type")
            if event_type == "thread.started" and event.get("thread_id"):
                session_id = str(event["thread_id"])
            elif event_type in {"turn.failed", "error"}:
                event_errors.append(json.dumps(event, default=str))
            elif event_type == "turn.completed":
                usage_data = event.get("usage", {}) or {}
            elif event_type == "item.completed":
                item = event.get("item", {})
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "agent_message":
                    messages.append(
                        Message(role=MessageRole.ASSISTANT, content=str(item.get("text", "")))
                    )
                elif item_type == "mcp_tool_call":
                    call_id = str(item.get("id", f"codex-tool-{len(messages)}"))
                    arguments = item.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {"value": arguments}
                    tool_name = str(item.get("tool", item.get("name", "")))
                    messages.append(
                        Message(
                            role=MessageRole.ASSISTANT,
                            tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=arguments)],
                        )
                    )
                    result_value = item.get("result", item.get("error", ""))
                    if not isinstance(result_value, str):
                        result_value = json.dumps(result_value, default=str)
                    messages.append(
                        Message(
                            role=MessageRole.TOOL,
                            content=result_value,
                            tool_call_id=call_id,
                        )
                    )

        combined_error = "\n".join([stderr, *event_errors])
        classified = classify_agent_exit(
            exit_code=(1 if event_errors and exit_code == 0 else exit_code),
            output=combined_error,
            timed_out=timed_out,
        )
        usage = Usage(
            prompt_tokens=int(usage_data.get("input_tokens", 0) or 0),
            completion_tokens=int(usage_data.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(usage_data.get("cached_input_tokens", 0) or 0),
        )
        finished = datetime.now(timezone.utc)
        trajectory = Trajectory(
            task_id=task_id,
            trial_index=trial_index,
            start_ts=started,
            end_ts=finished,
            status=classified.status,
            termination_reason=classified.reason,
            messages=messages,
            metrics=Metrics(
                latency_total_s=(finished - started).total_seconds(),
                turns=sum(1 for event in events if event.get("type") == "turn.started"),
                api_calls=sum(1 for event in events if event.get("type") == "turn.started"),
                usage=usage,
                tool_calls=sum(len(message.tool_calls or []) for message in messages),
            ),
        )
        final_event = {
            "num_turns": trajectory.metrics.turns,
            "total_cost_usd": 0.0,
        }
        return trajectory, messages_to_atif(
            messages,
            session_id=session_id,
            harness_type=self.config.type,
            harness_version=self.config.version,
            model_name=self.config.flags.get("model"),
            final_event=final_event,
        )

    def _convert_acp_stream(
        self,
        *,
        task_id: str,
        trial_index: int,
        instruction: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        started: datetime,
        timed_out: bool,
    ) -> tuple[Trajectory, dict[str, Any]]:
        """Convert generic ACP session updates into Tolokaforge and ATIF records."""

        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)

        messages = [Message(role=MessageRole.USER, content=instruction)]
        session_id = f"{task_id}:{trial_index}"
        text_chunks: list[str] = []
        pending_tools: dict[str, ToolCall] = {}
        prompt_tokens = 0
        completion_tokens = 0

        def flush_text() -> None:
            if text_chunks:
                messages.append(Message(role=MessageRole.ASSISTANT, content="".join(text_chunks)))
                text_chunks.clear()

        for event in events:
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if event.get("event_type") == "new_session":
                session_id = str(payload.get("sessionId", session_id))
                continue
            if event.get("event_type") != "session_update":
                continue
            update = payload.get("update", {})
            if not isinstance(update, dict):
                continue
            update_type = update.get("sessionUpdate")
            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    text_chunks.append(content["text"])
            elif update_type in {"tool_call", "tool_call_update"}:
                flush_text()
                call_id = str(update.get("toolCallId", f"acp-tool-{len(messages)}"))
                call = pending_tools.get(call_id)
                if call is None:
                    raw_input = update.get("rawInput", {})
                    arguments = raw_input if isinstance(raw_input, dict) else {"value": raw_input}
                    call = ToolCall(
                        id=call_id,
                        name=str(update.get("kind") or update.get("title") or "tool"),
                        arguments=arguments,
                    )
                    pending_tools[call_id] = call
                    messages.append(Message(role=MessageRole.ASSISTANT, tool_calls=[call]))
                if update.get("rawOutput") is not None:
                    output = update["rawOutput"]
                    if not isinstance(output, str):
                        output = json.dumps(output, default=str)
                    messages.append(
                        Message(role=MessageRole.TOOL, content=output, tool_call_id=call_id)
                    )
            elif update_type == "usage_update":
                prompt_tokens = int(update.get("inputTokens", prompt_tokens) or 0)
                completion_tokens = int(update.get("outputTokens", completion_tokens) or 0)
        flush_text()

        classified = classify_agent_exit(
            exit_code=exit_code,
            output=stderr,
            timed_out=timed_out,
        )
        finished = datetime.now(timezone.utc)
        usage = Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        trajectory = Trajectory(
            task_id=task_id,
            trial_index=trial_index,
            start_ts=started,
            end_ts=finished,
            status=classified.status,
            termination_reason=classified.reason,
            messages=messages,
            metrics=Metrics(
                latency_total_s=(finished - started).total_seconds(),
                turns=1,
                api_calls=1,
                usage=usage,
                tool_calls=sum(len(message.tool_calls or []) for message in messages),
            ),
        )
        return trajectory, messages_to_atif(
            messages,
            session_id=session_id,
            harness_type=self.config.type,
            harness_version=self.config.version,
            model_name=None,
            final_event={"num_turns": 1, "total_cost_usd": 0.0},
        )
