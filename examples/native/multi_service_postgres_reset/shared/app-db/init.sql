-- Factory schema + data for the `multi_service_postgres_reset` example.
--
-- Loaded by postgres:16's docker-entrypoint-initdb.d at container start
-- (once, before any client connects). Defines everything PostgREST
-- introspects at start-up, so the schema cache is populated before the
-- first request:
--   * `api` schema         — everything PostgREST exposes over HTTP.
--   * `web_anon` role      — the role PostgREST assumes for unauthenticated
--                            requests. NOLOGIN — only reachable via the
--                            authenticator's SET ROLE.
--   * `authenticator` role — the LOGIN role in POSTGRES_USER; PostgREST
--                            connects as this and switches to `web_anon`
--                            per request.
--
-- The `factory_default` row below is the value present until the reset
-- recipe overwrites it. If the recipe never fired, `GET /widgets` would
-- return `factory_default` and grading (which asserts `baseline`) would
-- fail — that gap is the whole point of the example.

CREATE SCHEMA api;

CREATE ROLE web_anon NOLOGIN;
GRANT web_anon TO authenticator;

GRANT USAGE ON SCHEMA api TO web_anon;

CREATE TABLE api.widgets (
  id   INT  PRIMARY KEY,
  name TEXT NOT NULL
);

GRANT SELECT ON api.widgets TO web_anon;

INSERT INTO api.widgets (id, name) VALUES (1, 'factory_default');
