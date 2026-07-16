-- Helpdesk-workflow schema + seed for the multi_service_helpdesk_workflow example.
--
-- Loaded once by postgres:16's docker-entrypoint-initdb.d at container start,
-- running as the POSTGRES_USER `app` role (owns the schema, read/write). The
-- five FastAPI services connect as `app`. A dedicated read-only `grader` role
-- (GRANT SELECT only) is the least-privilege oracle the db_probe DSN uses, so
-- grading reads the substrate directly and can never mutate it.
--
-- `crm_cases` ships empty on purpose: the agent creates the case the task asks
-- for, and grading verifies it landed with the policy-correct resolution path.

CREATE TABLE deliveries (
  delivery_id     INT  PRIMARY KEY,
  customer_id     TEXT NOT NULL,
  product_sku     TEXT NOT NULL,
  status          TEXT NOT NULL,
  original_eta    TIMESTAMPTZ NOT NULL,
  new_eta         TIMESTAMPTZ NOT NULL,
  temp_controlled BOOLEAN NOT NULL,
  resolution_path TEXT,
  resolution_note TEXT
);

CREATE TABLE products (
  sku                 TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  temp_sensitive      BOOLEAN NOT NULL,
  storage_requirement TEXT NOT NULL,
  max_hold_hours      INT  NOT NULL
);

CREATE TABLE sites (
  customer_id      TEXT PRIMARY KEY,
  site_name        TEXT NOT NULL,
  timezone         TEXT NOT NULL,
  staffed_until    TIME NOT NULL,
  has_temp_storage BOOLEAN NOT NULL,
  has_specialist   BOOLEAN NOT NULL
);

CREATE TABLE policy_docs (
  policy_id TEXT PRIMARY KEY,
  title     TEXT NOT NULL,
  body      TEXT NOT NULL,
  ts        TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || body)) STORED
);

CREATE INDEX policy_docs_ts_idx ON policy_docs USING GIN (ts);

CREATE TABLE crm_cases (
  case_id         SERIAL PRIMARY KEY,
  delivery_id     INT  NOT NULL,
  customer_id     TEXT NOT NULL,
  resolution_path TEXT NOT NULL,
  summary         TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO products (sku, name, temp_sensitive, storage_requirement, max_hold_hours) VALUES
  ('RGT-COLD-12', 'Cold-chain reagent kit',   TRUE,  '2-8C refrigerated', 6),
  ('RGT-STD-04',  'Standard assay reagent',   FALSE, 'ambient',           72),
  ('CNS-DRY-30',  'Dry consumables pack',     FALSE, 'ambient',           240),
  ('VAC-FRZ-09',  'Frozen vaccine lot',       TRUE,  'frozen -20C',       4);

INSERT INTO sites (customer_id, site_name, timezone, staffed_until, has_temp_storage, has_specialist) VALUES
  ('NORTHWIND', 'Northwind Biologics receiving dock', 'America/New_York', '17:00', FALSE, FALSE),
  ('CONTOSO',   'Contoso Labs cold room',             'America/Chicago',  '18:00', TRUE,  FALSE),
  ('FABRIKAM',  'Fabrikam Bio night-shift dock',      'America/Denver',   '22:00', FALSE, TRUE),
  ('WINGTIP',   'Wingtip Diagnostics main dock',      'America/New_York', '17:00', TRUE,  TRUE);

INSERT INTO deliveries
  (delivery_id, customer_id, product_sku, status, original_eta, new_eta, temp_controlled, resolution_path, resolution_note)
VALUES
  (4021, 'NORTHWIND', 'RGT-COLD-12', 'delayed',    '2026-07-16 14:00-04', '2026-07-16 20:00-04', TRUE,  NULL, NULL),
  (4022, 'CONTOSO',   'RGT-STD-04',  'in_transit', '2026-07-16 11:00-05', '2026-07-16 15:00-05', FALSE, NULL, NULL),
  (4023, 'FABRIKAM',  'VAC-FRZ-09',  'delayed',    '2026-07-16 16:00-06', '2026-07-16 21:00-06', TRUE,  NULL, NULL),
  (4024, 'WINGTIP',   'CNS-DRY-30',  'in_transit', '2026-07-16 09:00-04', '2026-07-16 13:00-04', FALSE, NULL, NULL);

INSERT INTO policy_docs (policy_id, title, body) VALUES
  ('POL-AH-01', 'After-hours receiving of temperature-sensitive shipments',
   'A temperature-sensitive shipment arriving outside a site''s staffed window may only be held on site when the site has certified temperature-controlled storage; where the receiving site has neither on-site temperature-controlled storage nor a certified receiving specialist, the shipment must be rescheduled to the next staffed window — a specialist handoff is not permitted without a certified specialist on site.'),
  ('POL-HOLD-02', 'On-site temperature-controlled hold',
   'When a receiving site is equipped with certified temperature-controlled storage, a delayed temperature-sensitive shipment may be held on site until the next staffed window, provided the product''s maximum hold time is not exceeded.'),
  ('POL-SPEC-03', 'Certified specialist handoff',
   'A late shipment may be handed off to a certified receiving specialist for after-hours acceptance when the site keeps a specialist on the late shift; the specialist assumes chain-of-custody until the dock reopens.'),
  ('POL-RESC-04', 'Rescheduling to the next staffed window',
   'Rescheduling returns a shipment to the carrier for redelivery at the start of the next staffed window. It is the default resolution when no compliant on-site handling option is available.'),
  ('POL-AMB-05', 'Ambient shipments after hours',
   'Shipments that are not temperature-sensitive may be left in the secure ambient holding area after hours and do not require a specialist or cold storage.'),
  ('POL-COLD-06', 'Cold-chain hold time limits',
   'Refrigerated reagents must return to 2-8C storage within their maximum hold hours; frozen products have shorter tolerances. Exceeding the hold time voids the shipment regardless of the resolution chosen.'),
  ('POL-CUST-07', 'Chain-of-custody documentation',
   'Every after-hours resolution must be recorded as a case with the customer, delivery, and the chosen resolution path so the receiving team has an auditable trail.'),
  ('POL-ESC-08', 'Escalation for perishable loss risk',
   'If no compliant resolution can keep a perishable shipment viable, escalate to the customer''s account manager before the product expires.'),
  ('POL-CARR-09', 'Carrier redelivery windows',
   'Carriers guarantee redelivery at the next staffed window when a reschedule is requested before the shipment reaches the dock.'),
  ('POL-STAFF-10', 'Staffed window definition',
   'A site''s staffed window is the interval during which qualified receiving personnel are on the dock; deliveries landing after this window are treated as after-hours arrivals.');

CREATE ROLE grader LOGIN PASSWORD 'grader_pw';
GRANT CONNECT ON DATABASE helpdesk TO grader;
GRANT USAGE ON SCHEMA public TO grader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grader;
