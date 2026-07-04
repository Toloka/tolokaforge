-- Support-triage schema for the `multi_service_postgres` example.
--
-- Loaded by postgres:16's docker-entrypoint-initdb.d at container start.
-- Runs exactly once during the first postgres init (before any client
-- connects); a re-run against an existing database is a no-op because
-- the compose stack tears down volumes on ``.stop(down=True)``.
--
-- Layout:
--   * ``api`` schema         — everything PostgREST exposes over HTTP.
--   * ``web_anon`` role      — the role PostgREST assumes for
--                              unauthenticated requests. NOLOGIN — only
--                              reachable via the authenticator's SET ROLE.
--   * ``authenticator`` role — the LOGIN role in POSTGRES_USER;
--                              PostgREST connects as this and switches
--                              to ``web_anon`` per request.
--
-- Both tables live in the ``api`` schema; PostgREST's
-- ``PGRST_DB_SCHEMAS=api`` restricts exposure to that schema only.

CREATE SCHEMA api;

CREATE ROLE web_anon NOLOGIN;
GRANT web_anon TO authenticator;

GRANT USAGE ON SCHEMA api TO web_anon;

CREATE TABLE api.customers (
  customer_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  tier        TEXT NOT NULL CHECK (tier IN ('enterprise', 'business', 'individual'))
);

CREATE TABLE api.tickets (
  ticket_id   TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES api.customers(customer_id),
  status      TEXT NOT NULL CHECK (status IN ('open', 'closed', 'resolved')),
  priority    INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
  subject     TEXT NOT NULL
);

GRANT SELECT ON api.customers TO web_anon;
GRANT SELECT ON api.tickets TO web_anon;

-- Six customers spanning all three tiers so the tier filter is a real
-- signal (not decorative).
INSERT INTO api.customers (customer_id, name, tier) VALUES
  ('C-101', 'Acme Corp',      'enterprise'),
  ('C-102', 'Beta Retail',    'business'),
  ('C-103', 'Corex Systems',  'enterprise'),
  ('C-104', 'Delta Labs',     'enterprise'),
  ('C-105', 'Halcyon Media',  'individual'),
  ('C-106', 'Enterprise Co',  'enterprise');

-- Ten tickets. Filtering to open + enterprise then ordering by priority
-- ascending yields a unique top-3: Acme Corp (P1) / Corex Systems (P2)
-- / Enterprise Co (P3). Distractors are deliberately placed so any
-- filter the agent skips would change the ranking:
--   * T-101 (Beta Retail, business, P1)   — top of unfiltered ranking
--                                            if agent skips tier filter.
--   * T-104 (Acme Corp, CLOSED, P1)       — top if agent skips status
--                                            filter (Acme appears twice).
--   * T-105 (Halcyon Media, individual, P1) — displaces Corex if tier
--                                              filter is skipped.
--   * T-108 (Delta Labs, RESOLVED, P2)    — displaces Corex if status
--                                            filter is skipped.
INSERT INTO api.tickets (ticket_id, customer_id, status, priority, subject) VALUES
  ('T-100', 'C-101', 'open',     1, 'Login failures spike'),
  ('T-101', 'C-102', 'open',     1, 'Checkout error'),
  ('T-102', 'C-103', 'open',     2, 'Data export timing out'),
  ('T-103', 'C-104', 'open',     4, 'Slow API response'),
  ('T-104', 'C-101', 'closed',   1, 'Old resolved billing question'),
  ('T-105', 'C-105', 'open',     1, 'Password reset stuck'),
  ('T-106', 'C-106', 'open',     3, 'SSO integration broken'),
  ('T-107', 'C-103', 'open',     5, 'Feature request'),
  ('T-108', 'C-104', 'resolved', 2, 'Auth fix landed'),
  ('T-109', 'C-106', 'open',     6, 'Docs typo');
