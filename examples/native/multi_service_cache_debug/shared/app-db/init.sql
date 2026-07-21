-- orders schema + seed for the multi_service_cache_debug example.
--
-- Loaded once by postgres:16's docker-entrypoint-initdb.d at container start as
-- the POSTGRES_USER `app` role. orders-api connects as `app` to read the source
-- of truth and apply status updates. No grader role: this pack grades a
-- diagnosis note, not a substrate mutation, so nothing reads postgres for grading.
--
-- order 4021 is seeded status='shipped' (the FRESH truth). The redis_dump seed
-- (assets/cache_poisoned.rdb) pre-loads order:4021 with status='processing'
-- (STALE). That divergence is the observable cache-invalidation bug.

CREATE TABLE orders (
  order_id    INT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  product     TEXT NOT NULL,
  status      TEXT NOT NULL,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO orders (order_id, customer_id, product, status) VALUES
  (4021, 'ACME',    'Widget crate',      'shipped'),
  (4022, 'GLOBEX',  'Sprocket bundle',   'processing'),
  (4023, 'INITECH', 'Gadget assortment', 'delivered');
