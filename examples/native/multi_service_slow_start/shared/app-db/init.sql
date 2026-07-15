-- Schema + data + slow-start driver for the `multi_service_slow_start`
-- example.
--
-- Loaded by postgres:16's docker-entrypoint-initdb.d at container start.
-- postgres runs every init script on a temporary socket-only server
-- (listen_addresses='') and only opens the real TCP listener after the
-- last script returns. The trailing `SELECT pg_sleep(25)` therefore holds
-- the container in a state where TCP :5432 is genuinely refused for ~25 s
-- — the honest failure surface the startup-order race would hit.
--
-- Everything PostgREST introspects is defined before the sleep, so the
-- schema and data are fully present by the time PostgREST (gated on
-- app-db healthy) connects:
--   * `api` schema         — everything PostgREST exposes over HTTP.
--   * `web_anon` role      — the role PostgREST assumes for unauthenticated
--                            requests. NOLOGIN — only reachable via the
--                            authenticator's SET ROLE.
--   * `authenticator` role — the LOGIN role in POSTGRES_USER; PostgREST
--                            connects as this and switches to `web_anon`
--                            per request.
--
-- Widget 1's `name = 'slow_start_ok'` is the distinctive probe value the
-- agent reads back over the REST API. The agent can only observe it if the
-- depends_on + healthcheck chain gated the start order — i.e. app-service
-- did not race ahead of postgres and the first API call did not hit a
-- refused connection. That gap is the whole point of the example.

CREATE SCHEMA api;

CREATE ROLE web_anon NOLOGIN;
GRANT web_anon TO authenticator;

GRANT USAGE ON SCHEMA api TO web_anon;

CREATE TABLE api.widgets (
  id   INT  PRIMARY KEY,
  name TEXT NOT NULL
);

GRANT SELECT ON api.widgets TO web_anon;

-- A small but real dataset so the queried DB is genuine, not a stub.
INSERT INTO api.widgets (id, name)
SELECT g, 'widget_' || g
FROM generate_series(2, 5000) AS g;

-- The distinctive probe row the task reads back.
INSERT INTO api.widgets (id, name) VALUES (1, 'slow_start_ok');

-- LAST: hold the socket-only init server for the slow-start window so the
-- real TCP listener stays down until schema + data are fully present.
SELECT pg_sleep(25);
