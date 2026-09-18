"""The proxy taps token usage in a real container, and the engine reads it back.

A harness trial spends its whole budget inside one tool call, so the engine
observes no LLM request and no token counts. ``kimi-code`` — the only shipped
harness declaring ``request_middleware`` — prints no totals of its own either,
which leaves the proxy on the wire as the only place the counts exist. The unit
suite drives the proxy's handler in-process and the engine's fold against a
scripted executor; this test runs the real script in a container, booted by the
argv the registry's harness command actually emits, and then drives the real
:class:`~tolokaforge.core.runner.TrialRunner` against that live container.

Three claims, and the third is the one nothing else can make:

1. a response reporting usage lands one NDJSON record at the fixed container
   path;
2. a response reporting none lands nothing — "not measured" has to stay
   distinguishable from "measured zero";
3. the trial's :class:`~tolokaforge.core.models.Metrics` carry exactly those
   counts, recovered **out of the still-running container**.

Claim 3 is why the container is held open rather than run to completion. The
records have no host path: the synthesised trial compose mounts ``/logs``
*relatively*, so a trial's writes land in the per-trial context copy the stack
deletes at teardown, and the staging tree keeps only the empty template. An
engine that read the host side would measure nothing and say so silently.

Hermetic: the upstream is a stub HTTP server inside the same container and the
container runs on ``--network none``, so nothing resolves, dials or bills a
real provider. The stub records every body it receives, which is also what
keeps the proxy's provider-pinning injection — the reason the middleware slot
exists at all — under real-container coverage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.models import TrialStatus
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult
from tolokaforge_coding_harnesses import (
    MIDDLEWARE_PROXY_CONTAINER_PATH,
    MIDDLEWARE_PROXY_SCRIPT,
    MIDDLEWARE_PROXY_USAGE_SOURCE,
    MIDDLEWARE_USAGE_LOG_CONTAINER_PATH,
    harness_command,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.requires_docker]

IMAGE = "python:3.12-slim"
STUB_PORT = 9101
CONTAINER_LOGS_DIR = "/logs"
UPSTREAM_RECORD_CONTAINER_PATH = f"{CONTAINER_LOGS_DIR}/upstream_requests.ndjson"
PROXY_URL_CONTAINER_PATH = f"{CONTAINER_LOGS_DIR}/proxy_base_url"
DRIVER_READY_CONTAINER_PATH = f"{CONTAINER_LOGS_DIR}/proxy_ready"
HARNESS_MODEL = "openrouter/moonshotai/kimi-k2.7-code"
DRIVER_READY_TIMEOUT_S = 60.0

REPORTING_MODEL = "reports-usage"
SILENT_MODEL = "silent"

EXPECTED_USAGE = {
    "prompt_tokens": 1301,
    "completion_tokens": 57,
    "total_tokens": 1358,
    "cache_read_input_tokens": 1024,
    "reasoning_tokens": 9,
}

PINNED_PROVIDER = {"only": ["moonshotai"], "allow_fallbacks": False}

# The canned provider responses. The usage-bearing one is shaped like an
# OpenRouter/OpenAI chat completion, details sub-objects included, so the
# proxy's extraction runs over the real envelope rather than a reduced one.
STUB_UPSTREAM = f'''\
"""Stand-in provider: OpenAI-shaped completions, one canned usage block."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_WITH_USAGE = {{
    "id": "chatcmpl-stub",
    "object": "chat.completion",
    "model": "stub",
    "choices": [
        {{
            "index": 0,
            "message": {{"role": "assistant", "content": "done"}},
            "finish_reason": "stop",
        }}
    ],
    "usage": {{
        "prompt_tokens": {EXPECTED_USAGE["prompt_tokens"]},
        "completion_tokens": {EXPECTED_USAGE["completion_tokens"]},
        "total_tokens": {EXPECTED_USAGE["total_tokens"]},
        "prompt_tokens_details": {{"cached_tokens": {EXPECTED_USAGE["cache_read_input_tokens"]}}},
        "completion_tokens_details": {{
            "reasoning_tokens": {EXPECTED_USAGE["reasoning_tokens"]}
        }},
    }},
}}

_WITHOUT_USAGE = {{
    key: value for key, value in _WITH_USAGE.items() if key != "usage"
}}

# The same spend, reported the way Google's ``generateContent`` reports it:
# ``candidatesTokenCount`` EXCLUDES thinking tokens, so it is the completion
# total minus the reasoning the OpenAI shape folds in.
_WITH_GEMINI_USAGE = {{
    "candidates": [{{"content": {{"parts": [{{"text": "done"}}], "role": "model"}}}}],
    "usageMetadata": {{
        "promptTokenCount": {EXPECTED_USAGE["prompt_tokens"]},
        "candidatesTokenCount": {
    EXPECTED_USAGE["completion_tokens"] - EXPECTED_USAGE["reasoning_tokens"]
},
        "thoughtsTokenCount": {EXPECTED_USAGE["reasoning_tokens"]},
        "totalTokenCount": {EXPECTED_USAGE["total_tokens"]},
        "cachedContentTokenCount": {EXPECTED_USAGE["cache_read_input_tokens"]},
    }},
}}

_WITHOUT_GEMINI_USAGE = {{
    key: value for key, value in _WITH_GEMINI_USAGE.items() if key != "usageMetadata"
}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--shape", default="openai", choices=["openai", "gemini"])
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *fmt_args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else {{}}
            with open(args.record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({{"path": self.path, "body": body}}) + "\\n")
            silent = body.get("model") == "{SILENT_MODEL}"
            if args.shape == "gemini":
                chosen = _WITHOUT_GEMINI_USAGE if silent else _WITH_GEMINI_USAGE
            else:
                chosen = _WITHOUT_USAGE if silent else _WITH_USAGE
            payload = json.dumps(chosen).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


main()
'''

CLIENT = '''\
"""Stand-in vendor CLI: one chat completion through whatever base URL it was
handed. Exits non-zero on anything but a 200 so the driver's ``set -e`` stops
the run at the first failure."""

from __future__ import annotations

import json
import sys
from urllib import request as urllib_request

base_url, model = sys.argv[1], sys.argv[2]
payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]})
req = urllib_request.Request(
    base_url.rstrip("/") + "/chat/completions",
    data=payload.encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib_request.urlopen(req, timeout=30) as response:
    status, body = response.status, response.read()
print(f"{model}: {status} {body!r}")
sys.exit(0 if status == 200 else 1)
'''

WAIT_PORT = '''\
"""Block until a TCP port accepts, so neither server can be raced."""

from __future__ import annotations

import socket
import sys
import time

port, deadline = int(sys.argv[1]), time.time() + 20.0
while time.time() < deadline:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
            sys.exit(0)
        except OSError:
            time.sleep(0.05)
raise SystemExit(f"port {port} never opened")
'''

# The proxy appends its record AFTER relaying the response, so a client that
# has its 200 is not yet evidence the record is on disk. Wait for the expected
# count, then hold a grace window so a record that should NOT have been written
# still has time to appear and fail the assertion instead of racing past it.
WAIT_RECORDS = '''\
"""Block until a file holds at least N lines, then let stragglers land."""

from __future__ import annotations

import os
import sys
import time

path, wanted, deadline = sys.argv[1], int(sys.argv[2]), time.time() + 30.0


def lines() -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        return len([line for line in handle if line.strip()])


while time.time() < deadline:
    if lines() >= wanted:
        time.sleep(1.0)
        sys.exit(0)
    time.sleep(0.1)
raise SystemExit(f"{path} never reached {wanted} line(s) (saw {lines()})")
'''


def _middleware_steps() -> list[str]:
    """The shipped kimi-code preamble steps, up to the base-URL rewrite.

    Taken from :func:`harness_command` rather than hand-written so the test
    boots the proxy exactly as a trial does — including the ``--usage-log``
    argv under test. The steps after the rewrite export the model name and
    invoke the vendor CLI, which is not installed here; the stand-in client
    takes its place, driven as the trial's own tool call.
    """
    steps = harness_command("kimi-code", "do it", HARNESS_MODEL).split(" && ")
    rewrite = next(
        i for i, step in enumerate(steps) if step.startswith("export KIMI_MODEL_BASE_URL")
    )
    return steps[: rewrite + 1]


def _driver_script(shape: str = "openai") -> str:
    """Boot the stub upstream and the real proxy, then hold the container open.

    Publishes the rewritten base URL to a file rather than leaving it in this
    shell's environment: the trial's tool call arrives as a fresh ``exec``, and
    reading the URL back keeps the registry the only place it is spelled.
    """
    steps = "\n".join(_middleware_steps())
    return f"""\
set -eu
export KIMI_MODEL_BASE_URL="http://127.0.0.1:{STUB_PORT}"
python3 /work/stub_upstream.py --port {STUB_PORT} \
--record {UPSTREAM_RECORD_CONTAINER_PATH} --shape {shape} &
python3 /work/wait_port.py {STUB_PORT}
{steps}
PROXY_PORT="${{KIMI_MODEL_BASE_URL##*:}}"
python3 /work/wait_port.py "${{PROXY_PORT%%/*}}"
printf '%s' "$KIMI_MODEL_BASE_URL" > {PROXY_URL_CONTAINER_PATH}
touch {DRIVER_READY_CONTAINER_PATH}
exec sleep 600
"""


def _harness_command() -> str:
    """What the trial runs as its single tool call, standing in for the CLI.

    Two provider requests through the proxy — one whose response reports usage
    and one whose response reports none — then a wait for the record the first
    must produce, so the trial's exec does not return before the tap has
    written and the engine's read cannot race it.
    """
    return (
        "set -eu\n"
        f'BASE_URL="$(cat {PROXY_URL_CONTAINER_PATH})"\n'
        f'python3 /work/client.py "$BASE_URL" {SILENT_MODEL}\n'
        f'python3 /work/client.py "$BASE_URL" {REPORTING_MODEL}\n'
        f"python3 /work/wait_records.py {MIDDLEWARE_USAGE_LOG_CONTAINER_PATH} 1\n"
    )


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class _StubAgentClient:
    """Stands in for the agent LLM client on the harness path.

    Only the model identity is read there — the CLI ran the model itself, so
    the client issues nothing.
    """

    model_name = HARNESS_MODEL


class _DockerExecToolExecutor:
    """The engine's exec seam, backed by ``docker exec`` on the live container.

    Stands exactly where ``DockerComposeExecToolWrapper`` stands in a real
    trial — ``bash -c`` into the running agent container — minus the compose
    project-name resolution, which this test has no compose project for. Every
    command is remembered so the test can assert the engine issued exactly the
    two executions it is supposed to: the CLI's, and its own read.
    """

    def __init__(self, container: str) -> None:
        self._container = container
        self.commands: list[str] = []

    def execute(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolResult:
        command = arguments["command"]
        self.commands.append(command)
        completed = subprocess.run(
            ["docker", "exec", self._container, "bash", "-c", command],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return ToolResult(
            success=completed.returncode == 0,
            output=completed.stdout,
            error=completed.stderr or None,
        )


def _await_driver_ready(container: str, marker: Path) -> None:
    deadline = time.time() + DRIVER_READY_TIMEOUT_S
    while time.time() < deadline:
        if marker.exists():
            return
        if not _is_running(container):
            logs = subprocess.run(
                ["docker", "logs", container], capture_output=True, text=True, check=False
            )
            raise AssertionError(
                f"driver exited before it was ready:\n{logs.stdout}\n{logs.stderr}"
            )
        time.sleep(0.1)
    raise AssertionError(f"driver never became ready within {DRIVER_READY_TIMEOUT_S:g}s")


def _is_running(container: str) -> bool:
    inspected = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return inspected.stdout.strip() == "true"


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (the proxy's usage tap needs a real container)",
)
@pytest.mark.parametrize(
    "shape",
    ["openai", "gemini"],
    ids=["openai-chat-completions", "google-generate-content"],
)
def test_the_proxy_taps_usage_and_the_trial_reads_it_out_of_the_container(
    tmp_path: Path, shape: str
) -> None:
    """Both wire shapes the proxy meters, end to end in a real container.

    The counts asserted below are identical for the two, which is the point:
    a record's meaning must not depend on which CLI produced it. The Gemini
    stub reports the same spend in Google's spelling, thinking tokens split
    out of the completion total the way Google splits them."""
    work_dir = tmp_path / "work"
    logs_dir = tmp_path / "logs"
    work_dir.mkdir()
    logs_dir.mkdir()

    # The real shipped script, at the path the harness command names.
    shutil.copy(MIDDLEWARE_PROXY_SCRIPT, work_dir / "middleware_proxy.py")
    (work_dir / "stub_upstream.py").write_text(STUB_UPSTREAM)
    (work_dir / "client.py").write_text(CLIENT)
    (work_dir / "wait_port.py").write_text(WAIT_PORT)
    (work_dir / "wait_records.py").write_text(WAIT_RECORDS)
    (work_dir / "drive.sh").write_text(_driver_script(shape))

    container = f"tolokaforge-proxy-tap-{uuid.uuid4().hex[:10]}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            # No egress at all: only loopback exists inside, so the proxy
            # cannot reach a real provider even if the stub URL were wrong.
            "--network",
            "none",
            "-v",
            f"{work_dir}:/work:ro",
            # The mount a synthesised harness compose gives the agent service.
            # Here it is a stable host directory, which lets the test watch the
            # driver's readiness marker; in a trial the same mount resolves
            # against a per-trial context copy teardown deletes, which is why
            # the engine reads the usage records back through ``exec`` below
            # rather than off the host.
            "-v",
            f"{logs_dir}:{CONTAINER_LOGS_DIR}",
            "-v",
            f"{work_dir / 'middleware_proxy.py'}:{MIDDLEWARE_PROXY_CONTAINER_PATH}:ro",
            IMAGE,
            "bash",
            "/work/drive.sh",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert started.returncode == 0, f"container did not start:\n{started.stderr}"

    try:
        _await_driver_ready(container, logs_dir / Path(DRIVER_READY_CONTAINER_PATH).name)

        executor = _DockerExecToolExecutor(container)
        runner = TrialRunner(
            task_id="middleware-proxy-tap",
            trial_index=0,
            agent_client=_StubAgentClient(),  # type: ignore[arg-type]
            user_simulator=None,
            tool_executor=executor,
            tool_schemas=[],
            episode_timeout_s=600,
        )
        trajectory = runner.run_harness(
            tool_name="bash",
            command=_harness_command(),
            instruction="do it",
            timeout_s=600,
            harness="kimi-code",
            usage_log_container_path=MIDDLEWARE_USAGE_LOG_CONTAINER_PATH,
        )
        usage_log = logs_dir / Path(MIDDLEWARE_USAGE_LOG_CONTAINER_PATH).relative_to(
            CONTAINER_LOGS_DIR
        )
        records = _records(usage_log)
        forwarded = _records(logs_dir / "upstream_requests.ndjson")
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)

    assert trajectory.status is TrialStatus.COMPLETED, trajectory.messages[-1].content

    # The tap wrote one record — the usage-bearing response's. A record for the
    # response without a usage block would report zero spend as if measured.
    assert len(records) == 1, f"expected exactly one usage record. Got: {records}"
    record = records[0]
    assert record["model"] == REPORTING_MODEL
    assert record["path"] == "/chat/completions"
    assert record["status"] == 200
    assert record["timestamp"].endswith("+00:00")
    assert {key: record[key] for key in EXPECTED_USAGE} == EXPECTED_USAGE

    # The engine recovered those counts out of the live container. This is the
    # claim a host-path read cannot make: nothing on the host is readable at
    # the path a real trial's compose mounts from.
    usage = trajectory.metrics.usage
    assert trajectory.metrics.harness_usage_source == MIDDLEWARE_PROXY_USAGE_SOURCE
    assert usage.prompt_tokens == EXPECTED_USAGE["prompt_tokens"]
    assert usage.completion_tokens == EXPECTED_USAGE["completion_tokens"]
    assert usage.cache_read_input_tokens == EXPECTED_USAGE["cache_read_input_tokens"]
    assert usage.reasoning_tokens == EXPECTED_USAGE["reasoning_tokens"]

    # Two executions ran in the container, and only the first was the agent's.
    assert executor.commands == [
        _harness_command(),
        f"cat -- {MIDDLEWARE_USAGE_LOG_CONTAINER_PATH}",
    ]
    assert trajectory.metrics.tool_calls == 1
    assert len(trajectory.tool_log) == 1

    # Both requests did reach the upstream, provider pinning injected: the tap
    # is additive to what the middleware already existed to do.
    assert [entry["body"]["model"] for entry in forwarded] == [SILENT_MODEL, REPORTING_MODEL]
    assert [entry["path"] for entry in forwarded] == ["/chat/completions"] * 2
    assert [entry["body"]["provider"] for entry in forwarded] == [PINNED_PROVIDER] * 2
