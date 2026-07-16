-- orders + customers substrate for the multi_service_endpoint_add example.
--
-- Loaded once by postgres:16's docker-entrypoint-initdb.d at container start as
-- the POSTGRES_USER `app` role. The seeded FastAPI service reads these tables;
-- the `GET /orders/{id}/summary` endpoint the agent adds joins an order onto its
-- customer, so both tables are seeded with matching keys.

CREATE TABLE customers (
  customer_id INT PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT NOT NULL,
  tier        TEXT NOT NULL
);

CREATE TABLE orders (
  order_id    INT PRIMARY KEY,
  customer_id INT NOT NULL REFERENCES customers(customer_id),
  product     TEXT NOT NULL,
  status      TEXT NOT NULL,
  amount      NUMERIC(10, 2) NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO customers (customer_id, name, email, tier) VALUES
  (1, 'Acme Industries',   'ops@acme.example',    'gold'),
  (2, 'Globex Corp',       'buyer@globex.example', 'silver'),
  (3, 'Initech LLC',       'proc@initech.example', 'bronze');

INSERT INTO orders (order_id, customer_id, product, status, amount) VALUES
  (5001, 1, 'Widget crate',      'shipped',    1299.00),
  (5002, 2, 'Sprocket bundle',   'processing',  480.50),
  (5003, 3, 'Gadget assortment', 'delivered',   215.75),
  (5004, 1, 'Cog set',           'processing',   99.99);
