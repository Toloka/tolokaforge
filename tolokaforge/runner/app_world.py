"""The runner's side of an application world served over HTTP (ADR-0058).

A task declaring ``initial_state.app_world`` keeps its state in a service of the
trial's stack rather than in an MCP server subprocess. At ``RegisterTrial`` the
runner mints the trial's credentials, claims the service with them, loads the
task's tables and hands each actor's ``http_request`` its bearer token
(:func:`open_app_world`). The :class:`AppWorldClient` it keeps answers the two
calls the runner already makes on an MCP server holding a trial's state —
``get_state()`` before grading and ``reset_state(tables)`` before the golden
replay — so grading treats both holders alike.

The administration protocol, behind ``X-Admin-Token``:

- ``PUT /_admin/tokens`` with ``{token: caller}``. The service starts with no admin
  token and binds the one the first such call carries; every later ``/_admin/*``
  call must present it, and is answered ``403`` otherwise.
- ``PUT /_admin/tables`` replaces the world with the given tables.
- ``GET /_admin/tables`` answers the world as ``{table: [record, ...]}``.

``GET /_health`` answers without a token, and every vendor request is answered
``503`` until tables are loaded.

Every minted value is admitted with :func:`register_runtime_secret` before it is
sent anywhere, so the process-wide log redactor masks it from then on.
"""

from __future__ import annotations

import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from tolokaforge.runner.models import AppWorldActor, AppWorldConfig
from tolokaforge.runner.tool_factory import BuiltinGenericToolWrapper
from tolokaforge.secrets import register_runtime_secret

ADMIN_TOKEN_HEADER = "X-Admin-Token"

ADMIN_TIMEOUT_S = 30.0
"""Budget of one administration call; loading a whole world is one request."""

_TOKEN_BYTES = 32

_HTTP_REQUEST_TOOL = "http_request"


@runtime_checkable
class BearerPresenter(Protocol):
    """A tool the runtime can hand a bearer token to present to some hosts alone.

    ``http_request`` is one; it is matched by this capability rather than imported,
    so the runner's import graph does not pull in every builtin tool's dependencies.
    """

    def present_bearer(self, token: str, hosts: Collection[str]) -> None: ...


class AppWorldError(RuntimeError):
    """The world service refused or failed an administration call."""


class AppWorldClaimLost(AppWorldError):
    """The world service is administered with an admin token this runner did not mint."""


@dataclass(frozen=True)
class AppWorldCredentials:
    """One trial's credentials for its world service, minted by the runner."""

    admin_token: str
    bearer_by_actor: Mapping[AppWorldActor, str]

    def callers(self, actors: Mapping[AppWorldActor, str | None]) -> dict[str, str | None]:
        """The ``{token: caller}`` body of ``PUT /_admin/tokens``."""
        return {self.bearer_by_actor[actor]: caller for actor, caller in actors.items()}


def mint_credentials(actors: Mapping[AppWorldActor, str | None]) -> AppWorldCredentials:
    """A fresh admin token and one bearer token per actor, each admitted for redaction.

    The secret names carry a nonce of their own: a trial id is re-registered after
    ``CleanupTrial`` with new values, and one name never takes two values.
    """
    nonce = secrets.token_hex(8)
    admin_token = _admitted(f"APP_WORLD_{nonce}_ADMIN_TOKEN")
    bearer_by_actor = {
        actor: _admitted(f"APP_WORLD_{nonce}_{actor.upper()}_TOKEN") for actor in actors
    }
    return AppWorldCredentials(admin_token=admin_token, bearer_by_actor=bearer_by_actor)


def _admitted(name: str) -> str:
    value = secrets.token_urlsafe(_TOKEN_BYTES)
    register_runtime_secret(name, value)
    return value


class AppWorldClient:
    """Administers one trial's world service: claim, load, read back, restore."""

    def __init__(
        self,
        config: AppWorldConfig,
        admin_token: str,
        *,
        timeout_s: float = ADMIN_TIMEOUT_S,
    ) -> None:
        self.config = config
        self._client = httpx.Client(
            base_url=config.url,
            headers={ADMIN_TOKEN_HEADER: admin_token},
            timeout=timeout_s,
        )

    def claim(self, callers: Mapping[str, str | None]) -> None:
        """Bind the admin token and load the bearer tokens, in the service's first admin call.

        Raises:
            AppWorldClaimLost: the service answered ``403`` — another admin token is
                already bound, so the world is not this trial's to grade.
            AppWorldError: the service failed the call otherwise.
        """
        response = self._send("PUT", "/_admin/tokens", dict(callers))
        if response.status_code == httpx.codes.FORBIDDEN:
            raise AppWorldClaimLost(
                f"world service {self.config.service!r} at {self.config.url} refused the "
                "runner's admin token: another admin token was bound first, so the world is "
                "not this trial's. A container of the stack called PUT /_admin/tokens before "
                "RegisterTrial; the world service must start unclaimed for every trial"
            )
        self._refuse_failure("PUT", "/_admin/tokens", response)

    def get_state(self) -> dict[str, list[dict[str, Any]]]:
        """The world as ``{table: [record, ...]}``, as an MCP state holder answers it."""
        state = self._call("GET", "/_admin/tables")
        if not isinstance(state, dict) or not all(
            isinstance(records, list) for records in state.values()
        ):
            raise AppWorldError(
                f"world service {self.config.service!r} answered GET /_admin/tables with "
                f"{type(state).__name__}, not a mapping of table name to a list of records"
            )
        return state

    def reset_state(self, initial_state: Mapping[str, Any]) -> None:
        """Replace the world with ``initial_state`` (the task's initial tables)."""
        self._call("PUT", "/_admin/tables", dict(initial_state))

    def close(self) -> None:
        self._client.close()

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        response = self._send(method, path, payload)
        self._refuse_failure(method, path, response)
        return response.json() if method == "GET" else None

    def _send(self, method: str, path: str, payload: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, json=payload)
        except httpx.HTTPError as error:
            raise AppWorldError(
                f"world service {self.config.service!r} at {self.config.url} is unreachable "
                f"for {method} {path}: {error}"
            ) from error

    def _refuse_failure(self, method: str, path: str, response: httpx.Response) -> None:
        if response.is_success:
            return
        raise AppWorldError(
            f"world service {self.config.service!r} at {self.config.url} refused {method} "
            f"{path}: HTTP {response.status_code} {response.text[:500]}"
        )


def open_app_world(
    config: AppWorldConfig,
    tables: Mapping[str, Any],
    tools_by_actor: Mapping[AppWorldActor, Mapping[str, Any]],
) -> AppWorldClient:
    """Mint the trial's credentials, claim the service, load the world, arm ``http_request``.

    Tokens are loaded before tables, so the world is never served without its
    callers. Each actor's ``http_request`` presents its own bearer token, and only
    to ``config.hosts``.

    Raises:
        AppWorldError: an actor has no ``http_request`` to present its token, or the
            service refused a call (:class:`AppWorldClaimLost` for a lost claim).
    """
    http_tools = {actor: _http_request_tool(actor, tools_by_actor) for actor in config.actors}
    credentials = mint_credentials(config.actors)
    client = AppWorldClient(config, credentials.admin_token)
    try:
        client.claim(credentials.callers(config.actors))
        client.reset_state(tables)
        for actor, tool in http_tools.items():
            tool.present_bearer(credentials.bearer_by_actor[actor], config.hosts)
    except AppWorldError:
        client.close()
        raise
    except ValueError as error:
        client.close()
        raise AppWorldError(f"initial_state.app_world: {error}") from error
    return client


def _http_request_tool(
    actor: AppWorldActor, tools_by_actor: Mapping[AppWorldActor, Mapping[str, Any]]
) -> BearerPresenter:
    wrapper = tools_by_actor[actor].get(_HTTP_REQUEST_TOOL)
    tool = wrapper.builtin_tool if isinstance(wrapper, BuiltinGenericToolWrapper) else None
    if not isinstance(tool, BearerPresenter):
        raise AppWorldError(
            f"initial_state.app_world.actors names {actor!r}, which has no builtin "
            f"{_HTTP_REQUEST_TOOL} to present its token with; enable it in "
            f"tools.{actor}.enabled"
        )
    return tool
