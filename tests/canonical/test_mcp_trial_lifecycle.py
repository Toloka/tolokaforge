"""Real stdio children: trial isolation, concurrent calls and cancellation teardown."""

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.tool_factory import MCPServerPool, MCPServerProcess, ToolFactory

pytestmark = pytest.mark.canonical

SERVER = """
import json, sys, time
counter = 0
for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req:
        continue
    if req['method'] == 'initialize':
        result = {}
    else:
        args = req['params']['arguments']
        if args.get('ready_file'):
            with open(args['ready_file'], 'w') as ready:
                ready.write('ready')
        time.sleep(args.get('sleep', 0))
        counter += 1
        result = {'content': [{'type': 'text', 'text': json.dumps({'counter': counter, 'tag': args.get('tag')})}]}
    print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)
"""


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "server.py"
    path.write_text(SERVER)
    return str(path)


def tools(script, trial):
    schema = {
        "name": "counter",
        "description": "counter",
        "parameters": {"type": "object", "properties": {}},
        "source": {
            "toolset": "test_server",
            "module_path": "server",
            "class_name": "counter",
            "invocation_style": "mcp_server",
            "mcp_server_script": script,
        },
    }
    return ToolFactory(DBServiceClient("http://db-service.invalid"), trial).reconstruct_tools(
        [schema], [schema]
    )


async def test_agent_user_share_a_child_but_parallel_trials_do_not(script):
    first, second = tools(script, "a"), tools(script, "b")
    try:
        a = first.agent_tools["counter"]
        u = first.user_tools["counter"]
        b = second.agent_tools["counter"]
        assert json.loads(await a.execute({}))["counter"] == 1
        assert json.loads(await u.execute({}))["counter"] == 2
        assert json.loads(await b.execute({}))["counter"] == 1
        child_a, child_b = a._get_server().process, b._get_server().process
        assert child_a.pid != child_b.pid
        first.cleanup()
        first.cleanup()
        assert child_a.poll() is not None
        assert all(stream.closed for stream in (child_a.stdin, child_a.stdout, child_a.stderr))
        assert child_b.poll() is None
        assert json.loads(await b.execute({}))["counter"] == 2
        with pytest.raises(RuntimeError, match="closed"):
            await a.execute({})
    finally:
        first.cleanup()
        second.cleanup()
    assert child_b.poll() is not None


async def test_concurrent_tools_receive_their_own_responses(script):
    owned = tools(script, "parallel")
    try:
        wrapper = owned.agent_tools["counter"]
        replies = await asyncio.gather(*(wrapper.execute({"tag": n}) for n in range(20)))
        assert [json.loads(r)["tag"] for r in replies] == list(range(20))
        assert sorted(json.loads(r)["counter"] for r in replies) == list(range(1, 21))
    finally:
        owned.cleanup()


def test_cleanup_reaps_a_child_and_unblocks_its_pending_request(script, tmp_path):
    server = MCPServerProcess(script_path=script)
    server.start()
    process = server.process
    with ThreadPoolExecutor(max_workers=1) as executor:
        ready = tmp_path / "ready"
        pending = executor.submit(
            server.send_request,
            "tools/call",
            {"arguments": {"sleep": 300, "ready_file": str(ready)}},
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child never received the pending request"
        server.stop()
        with pytest.raises(RuntimeError, match="closed connection"):
            pending.result(timeout=5)
    assert process.poll() is not None
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    server.stop()


def test_failed_initialize_reaps_child_and_closes_pipes(tmp_path):
    script = tmp_path / "bad.py"
    script.write_text(
        "import sys; sys.stdin.readline(); print('not json', flush=True); sys.stdin.read()"
    )
    server = MCPServerProcess(script_path=str(script))
    with pytest.raises(json.JSONDecodeError):
        server.start()
    assert server.process is None
    server.stop()


def test_initialize_refuses_a_response_for_another_request(tmp_path):
    script = tmp_path / "wrong_id.py"
    script.write_text(
        "import json, sys; req = json.loads(sys.stdin.readline()); "
        "print(json.dumps({'jsonrpc': '2.0', 'id': req['id'] + 1, 'result': {}}), "
        "flush=True); sys.stdin.read()"
    )
    server = MCPServerProcess(script_path=str(script))
    with pytest.raises(RuntimeError, match="does not match request"):
        server.start()
    assert server.process is None


def test_failed_cleanup_keeps_child_owned_until_retry(script):
    class RefuseFirstStop(MCPServerProcess):
        refuse: bool = True

        def stop(self):
            if self.refuse:
                self.refuse = False
                raise PermissionError("injected teardown refusal")
            super().stop()

    pool = MCPServerPool()
    child = RefuseFirstStop(script_path=script)
    pool._servers[script] = child
    pool.get_server(script)
    process = child.process
    try:
        with pytest.raises(RuntimeError, match="injected teardown refusal"):
            pool.cleanup()
        assert process.poll() is None
        with pytest.raises(RuntimeError, match="closed"):
            pool.get_server(script)
        pool.cleanup()
        assert process.poll() is not None
        assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    finally:
        child.stop()
