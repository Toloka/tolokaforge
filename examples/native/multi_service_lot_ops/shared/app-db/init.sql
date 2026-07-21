-- Manufacturing schema + seed for the multi_service_lot_ops example.
--
-- Loaded once by postgres:16's docker-entrypoint-initdb.d at container start,
-- running as the POSTGRES_USER `app` role (owns the schema, read/write). The
-- FastAPI app-service connects as `app`. A dedicated read-only `grader` role
-- (GRANT SELECT only) is the least-privilege oracle the db_probe DSN uses, so
-- grading reads the substrate directly and can never mutate it — mirrors the
-- web_anon / authenticator split in the sibling PostgREST packs.
--
-- `corrective_actions` ships empty on purpose: the agent creates the row the
-- task asks for, and grading verifies it landed in this table.

CREATE TABLE reason_codes (
  code        TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  category    TEXT NOT NULL
);

CREATE TABLE lots (
  lot_id      INT  PRIMARY KEY,
  lot_code    TEXT NOT NULL UNIQUE,
  product     TEXT NOT NULL,
  status      TEXT NOT NULL,
  quantity    INT  NOT NULL,
  created_at  DATE NOT NULL
);

CREATE TABLE production_orders (
  order_id    INT  PRIMARY KEY,
  order_code  TEXT NOT NULL UNIQUE,
  lot_id      INT  NOT NULL REFERENCES lots(lot_id),
  status      TEXT NOT NULL,
  quantity    INT  NOT NULL,
  due_date    DATE NOT NULL
);

CREATE TABLE corrective_actions (
  ca_id       SERIAL PRIMARY KEY,
  lot_id      INT  NOT NULL REFERENCES lots(lot_id),
  reason_code TEXT NOT NULL REFERENCES reason_codes(code),
  note        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE allocations (
  alloc_id    SERIAL PRIMARY KEY,
  order_id    INT NOT NULL REFERENCES production_orders(order_id),
  lot_id      INT NOT NULL REFERENCES lots(lot_id),
  quantity    INT NOT NULL
);

INSERT INTO reason_codes (code, title, category) VALUES
  ('CAPA-01', 'Contamination', 'quality'),
  ('CAPA-02', 'Dimensional nonconformance', 'quality'),
  ('CAPA-03', 'Documentation error', 'process');

INSERT INTO lots (lot_id, lot_code, product, status, quantity, created_at) VALUES
  (1,  'LOT-1001', 'Buffer solution A', 'released',    480, '2026-06-02'),
  (2,  'LOT-1002', 'Buffer solution A', 'released',    500, '2026-06-04'),
  (3,  'LOT-1003', 'Reagent kit B',     'on_hold',     220, '2026-06-07'),
  (4,  'LOT-1004', 'Reagent kit B',     'released',    240, '2026-06-09'),
  (5,  'LOT-1005', 'Sterile vial C',    'released',   1000, '2026-06-11'),
  (6,  'LOT-1006', 'Sterile vial C',    'quarantined', 950, '2026-06-14'),
  (7,  'LOT-1007', 'Sterile vial C',    'released',    980, '2026-06-16'),
  (8,  'LOT-1008', 'Buffer solution A', 'released',    460, '2026-06-18'),
  (9,  'LOT-1009', 'Reagent kit B',     'on_hold',     210, '2026-06-21'),
  (10, 'LOT-1010', 'Sterile vial C',    'released',    990, '2026-06-24');

INSERT INTO production_orders (order_id, order_code, lot_id, status, quantity, due_date) VALUES
  (1, 'PO-5001', 1, 'closed', 480, '2026-06-20'),
  (2, 'PO-5002', 5, 'open',   600, '2026-07-05'),
  (3, 'PO-5003', 7, 'open',   700, '2026-07-12'),
  (4, 'PO-5004', 8, 'open',   400, '2026-07-15'),
  (5, 'PO-5005', 4, 'closed', 240, '2026-06-28');

INSERT INTO allocations (order_id, lot_id, quantity) VALUES
  (1, 1, 480),
  (2, 5, 600),
  (3, 7, 700),
  (5, 4, 240);

CREATE ROLE grader LOGIN PASSWORD 'grader_pw';
GRANT CONNECT ON DATABASE mfg TO grader;
GRANT USAGE ON SCHEMA public TO grader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grader;
