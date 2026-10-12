"""``DBServiceClient`` and the db-service app agree on the routes, and every data route is per trial.

Two halves, both read from source rather than a running service:

* every request ``DBServiceClient`` sends (each ``client.<verb>(...)`` call in
  ``runner/db_client.py``) names a method and path template the db-service
  FastAPI app serves;
* every route the app serves, other than ``/health``, sits under
  ``/trials/{trial_id}``, so no flat route reaches a store shared by trials.

Path fields compare by position, not name: the client's ``{name}`` matches the
app's ``{snapshot_name}``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from tolokaforge.env.json_db_service.app import app as db_app

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DB_CLIENT_SOURCE = _REPO_ROOT / "tolokaforge" / "runner" / "db_client.py"
_HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete"})
_UNSCOPED_ROUTES = frozenset({"/health"})
_TRIAL_PREFIX = "/trials/{trial_id}"
_NON_REQUEST_METHODS = frozenset({"close"})


def _path_template(node: ast.expr, where: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    pytest.fail(f"{where}: request path is not a string literal or f-string: {ast.dump(node)}")


def _positional(template: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", template)


def _client_requests_by_method() -> dict[str, list[tuple[str, str]]]:
    tree = ast.parse(_DB_CLIENT_SOURCE.read_text())
    client_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DBServiceClient"
    )
    requests: dict[str, list[tuple[str, str]]] = {}
    for method in client_class.body:
        if not isinstance(method, ast.AsyncFunctionDef) or method.name.startswith("__"):
            continue
        if method.name in _NON_REQUEST_METHODS:
            continue
        requests[method.name] = [
            (call.func.attr.upper(), _path_template(call.args[0], method.name))
            for call in ast.walk(method)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in _HTTP_VERBS
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "client"
        ]
    return requests


def _served_routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in db_app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_every_request_the_client_sends_is_a_served_route() -> None:
    requests = _client_requests_by_method()
    silent = sorted(name for name, sent in requests.items() if not sent)
    assert not silent, (
        f"DBServiceClient methods {silent} send no `client.<verb>(path)` request this "
        "guard can read; route them through the same call shape so their paths are checked"
    )
    served = {(method, _positional(path)) for method, path in _served_routes()}
    unserved = sorted(
        f"{name}: {verb} {path}"
        for name, sent in requests.items()
        for verb, path in sent
        if (verb, _positional(path)) not in served
    )
    assert not unserved, f"DBServiceClient sends requests db-service does not serve: {unserved}"


def test_every_served_data_route_is_trial_scoped() -> None:
    flat = sorted(
        f"{method} {path}"
        for method, path in _served_routes()
        if path not in _UNSCOPED_ROUTES
        and path != _TRIAL_PREFIX
        and not path.startswith(_TRIAL_PREFIX + "/")
    )
    assert not flat, (
        f"db-service serves routes outside {_TRIAL_PREFIX}: {flat}. A route without a "
        "trial id reaches a store every trial shares; address the trial instead"
    )
