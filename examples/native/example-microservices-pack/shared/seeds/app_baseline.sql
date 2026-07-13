-- app_baseline seed — the named baseline the postgres service resets
-- to between trials (see project.yaml: assets.seeds.app_baseline and
-- default_environment.services.postgres.reset).
--
-- Schema + a small representative dataset for the backend-api's
-- orders domain. Kept intentionally tiny; tasks that need volume
-- (db_query_tuning) generate additional load in-trial.

CREATE TABLE customers (
    id         SERIAL PRIMARY KEY,
    tenant     TEXT        NOT NULL,
    name       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER     NOT NULL REFERENCES customers (id),
    status      TEXT        NOT NULL DEFAULT 'pending',
    total_cents INTEGER     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX orders_customer_id_idx ON orders (customer_id);

INSERT INTO customers (tenant, name) VALUES
    ('acme',  'Ada Lovelace'),
    ('acme',  'Grace Hopper'),
    ('globex', 'Alan Turing');

INSERT INTO orders (customer_id, status, total_cents) VALUES
    (1, 'pending',   1999),
    (1, 'shipped',  25000),
    (2, 'pending',    499),
    (3, 'cancelled', 7500);
