-- Billing service schema and seed data

CREATE TABLE IF NOT EXISTS clients (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(255) NOT NULL,
    email    VARCHAR(255) NOT NULL,
    balance  NUMERIC(15, 2) DEFAULT 0,
    fee_type VARCHAR(20)   NOT NULL DEFAULT 'percentage'
);

CREATE TABLE IF NOT EXISTS holds (
    id               SERIAL PRIMARY KEY,
    client_id        INTEGER NOT NULL REFERENCES clients(id),
    amount           NUMERIC(15, 2) NOT NULL,
    fee_rate         NUMERIC(8, 4)  NOT NULL,
    fee_charged      NUMERIC(15, 2),
    committed_amount NUMERIC(15, 2),
    status           VARCHAR(20)    NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMP,
    committed_at     TIMESTAMP,
    cancelled_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id               SERIAL PRIMARY KEY,
    hold_id          INTEGER NOT NULL REFERENCES holds(id),
    client_id        INTEGER NOT NULL REFERENCES clients(id),
    transaction_type VARCHAR(20)    NOT NULL,
    amount           NUMERIC(15, 2) NOT NULL,
    created_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS platform_settings (
    key   VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL
);

-- Per-client fee rate overrides (overrides platform default when present)
-- fee_rate_pct is stored as percentage integer: 5 means 5%
CREATE TABLE IF NOT EXISTS client_fee_overrides (
    id           SERIAL PRIMARY KEY,
    client_id    INTEGER NOT NULL REFERENCES clients(id),
    fee_rate_pct NUMERIC(8, 4) NOT NULL,
    valid_from   TIMESTAMP NOT NULL,
    valid_to     TIMESTAMP,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS report_summaries (
    id                SERIAL PRIMARY KEY,
    client_id         INTEGER NOT NULL REFERENCES clients(id),
    period_date       VARCHAR(10) NOT NULL,
    total_held        NUMERIC(15, 2) DEFAULT 0,
    total_committed   NUMERIC(15, 2) DEFAULT 0,
    total_cancelled   NUMERIC(15, 2) DEFAULT 0,
    total_fees        NUMERIC(15, 2) DEFAULT 0,
    transaction_count INTEGER        DEFAULT 0
);

-- Cumulative fee revenue tracking per client (used by finance team for dashboards)
-- Updated whenever holds are committed or migrations run
CREATE TABLE IF NOT EXISTS client_fee_totals (
    client_id     INTEGER PRIMARY KEY REFERENCES clients(id),
    lifetime_fees NUMERIC(15, 2) DEFAULT 0,
    ytd_fees      NUMERIC(15, 2) DEFAULT 0,
    last_updated  TIMESTAMP
);

-- Financial audit ledger (append-only by design — used for regulatory compliance)
CREATE TABLE IF NOT EXISTS billing_ledger (
    id         SERIAL PRIMARY KEY,
    hold_id    INTEGER NOT NULL REFERENCES holds(id),
    client_id  INTEGER NOT NULL REFERENCES clients(id),
    entry_type VARCHAR(30) NOT NULL,
    amount     NUMERIC(15, 2) NOT NULL,
    notes      TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Enforce immutability: billing_ledger entries may not be modified or deleted
CREATE OR REPLACE FUNCTION _billing_ledger_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'billing_ledger is an append-only audit log — rows cannot be updated or deleted';
END;
$$;

DROP TRIGGER IF EXISTS trg_billing_ledger_no_update ON billing_ledger;
CREATE TRIGGER trg_billing_ledger_no_update
    BEFORE UPDATE OR DELETE ON billing_ledger
    FOR EACH ROW EXECUTE FUNCTION _billing_ledger_immutable();

-- Platform configuration
-- fee_rate:     used by 'percentage' fee type clients (decimal fraction: 0.05 = 5%)
-- flat_rate_fee: used by 'flat_rate' fee type clients (fixed dollar amount per transaction)
INSERT INTO platform_settings (key, value) VALUES
    ('fee_rate',     '0.05'),
    ('flat_rate_fee', '25.00');

-- -----------------------------------------------------------------------
-- Clients — each assigned a different fee calculation type
--   Alice Corp  (id=1): percentage — fee = amount * fee_rate
--   Bob Ltd     (id=2): tiered     — fee depends on amount brackets
--   Charlie Inc (id=3): flat_rate  — fee is a fixed $25 per transaction
-- -----------------------------------------------------------------------
INSERT INTO clients (id, name, email, balance, fee_type) VALUES
    (1, 'Alice Corp',  'alice@corp.com',   50000.00, 'percentage'),
    (2, 'Bob Ltd',     'bob@ltd.com',      75000.00, 'tiered'),
    (3, 'Charlie Inc', 'charlie@inc.com',  30000.00, 'flat_rate');

-- Reset sequence after manual id inserts
SELECT setval('clients_id_seq', (SELECT MAX(id) FROM clients));

-- -----------------------------------------------------------------------
-- Holds from 2024-01-01 to 2024-01-14  (pre-bug period — all correct)
-- -----------------------------------------------------------------------

-- Hold 1: Alice Corp (percentage), $2000, committed 2024-01-05
-- fee = 2000 * 0.05 = 100.00, committed = 1900.00
INSERT INTO holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
                   status, created_at, committed_at) VALUES
    (1, 1, 2000.00, 0.05, 100.00, 1900.00, 'committed',
     '2024-01-03 09:00:00', '2024-01-05 10:00:00');

-- Hold 2: Bob Ltd (tiered), $4000, committed 2024-01-10
-- Tiered correct: $4000 in tier $1001-$10000 → 5%: fee = 200.00, committed = 3800.00
-- fee_rate=0 since tiered clients don't store a rate in holds
INSERT INTO holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
                   status, created_at, committed_at) VALUES
    (2, 2, 4000.00, 0.00, 200.00, 3800.00, 'committed',
     '2024-01-08 11:00:00', '2024-01-10 14:00:00');

-- -----------------------------------------------------------------------
-- Holds from 2024-01-15 to 2024-02-28  (bug period — percentage & tiered affected)
-- A bad deployment on 2024-01-15 introduced fee calculation bugs.
-- -----------------------------------------------------------------------

-- Hold 3: Alice Corp (percentage), $5000, committed 2024-01-20
-- WRONG: fee = 5000 * 0.05 / 100 = 2.50   (bug: extra /100 division)
-- Correct: fee = 5000 * 0.05 = 250.00
INSERT INTO holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
                   status, created_at, committed_at) VALUES
    (3, 1, 5000.00, 0.05, 2.50, 4997.50, 'committed',
     '2024-01-18 08:30:00', '2024-01-20 09:00:00');

-- Hold 4: Bob Ltd (tiered), $3000, committed 2024-02-01
-- WRONG: bug uses wrong tier thresholds (100/1000 instead of 1000/10000)
--        $3000 > $1000 → 8% → fee = 3000 * 0.08 = 240.00
-- Correct: $3000 in $1001-$10000 → 5% → fee = 3000 * 0.05 = 150.00
INSERT INTO holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
                   status, created_at, committed_at) VALUES
    (4, 2, 3000.00, 0.00, 240.00, 2760.00, 'committed',
     '2024-01-30 10:00:00', '2024-02-01 11:00:00');

-- Hold 5: Charlie Inc (flat_rate), $1500, committed 2024-02-15
-- flat_rate is NOT buggy — fee = $25.00 (flat), committed = 1475.00
INSERT INTO holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
                   status, created_at, committed_at) VALUES
    (5, 3, 1500.00, 25.00, 25.00, 1475.00, 'committed',
     '2024-02-12 13:00:00', '2024-02-15 15:00:00');

-- Hold 6: Alice Corp (percentage), $800, committed 2024-02-20
-- WRONG: fee = 800 * 0.05 / 100 = 0.40   (bug: extra /100 division)
-- Correct: fee = 800 * 0.05 = 40.00
INSERT INTO holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
                   status, created_at, committed_at) VALUES
    (6, 1, 800.00, 0.05, 0.40, 799.60, 'committed',
     '2024-02-18 16:00:00', '2024-02-20 17:00:00');

-- -----------------------------------------------------------------------
-- Holds AFTER the bug period (2024-03-01+) — pending/cancelled, no commit calc
-- -----------------------------------------------------------------------

-- Hold 7: Bob Ltd (tiered), $1000, still pending
INSERT INTO holds (id, client_id, amount, fee_rate, status, created_at) VALUES
    (7, 2, 1000.00, 0.00, 'pending', '2024-03-10 09:00:00');

-- Hold 8: Charlie Inc (flat_rate), $600, cancelled
INSERT INTO holds (id, client_id, amount, fee_rate, status, created_at, cancelled_at) VALUES
    (8, 3, 600.00, 25.00, 'cancelled', '2024-03-12 10:00:00', '2024-03-13 11:00:00');

SELECT setval('holds_id_seq', (SELECT MAX(id) FROM holds));

-- -----------------------------------------------------------------------
-- Transactions (one "hold" tx per hold, one "commit" or "cancel" tx per outcome)
-- -----------------------------------------------------------------------

-- Hold 1 transactions (correct)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (1, 1, 'hold',   2000.00, '2024-01-03 09:00:00'),
    (1, 1, 'commit', 1900.00, '2024-01-05 10:00:00');

-- Hold 2 transactions (correct)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (2, 2, 'hold',   4000.00, '2024-01-08 11:00:00'),
    (2, 2, 'commit', 3800.00, '2024-01-10 14:00:00');

-- Hold 3 transactions (wrong committed amount due to percentage bug)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (3, 1, 'hold',   5000.00, '2024-01-18 08:30:00'),
    (3, 1, 'commit', 4997.50, '2024-01-20 09:00:00');

-- Hold 4 transactions (wrong committed amount due to tiered bug)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (4, 2, 'hold',   3000.00, '2024-01-30 10:00:00'),
    (4, 2, 'commit', 2760.00, '2024-02-01 11:00:00');

-- Hold 5 transactions (correct — flat_rate was not buggy)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (5, 3, 'hold',   1500.00, '2024-02-12 13:00:00'),
    (5, 3, 'commit', 1475.00, '2024-02-15 15:00:00');

-- Hold 6 transactions (wrong committed amount due to percentage bug)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (6, 1, 'hold',    800.00, '2024-02-18 16:00:00'),
    (6, 1, 'commit',  799.60, '2024-02-20 17:00:00');

-- Hold 7 transactions (pending — only hold tx)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (7, 2, 'hold', 1000.00, '2024-03-10 09:00:00');

-- Hold 8 transactions (cancelled)
INSERT INTO transactions (hold_id, client_id, transaction_type, amount, created_at) VALUES
    (8, 3, 'hold',   600.00, '2024-03-12 10:00:00'),
    (8, 3, 'cancel', 600.00, '2024-03-13 11:00:00');

-- -----------------------------------------------------------------------
-- Pre-aggregated report_summaries (reflects buggy committed amounts)
-- -----------------------------------------------------------------------

-- Pre-bug period summaries (correct)
INSERT INTO report_summaries (client_id, period_date, total_held, total_committed, total_cancelled, total_fees, transaction_count) VALUES
    (1, '2024-01-05', 2000.00, 1900.00, 0.00, 100.00, 1),
    (2, '2024-01-10', 4000.00, 3800.00, 0.00, 200.00, 1);

-- Bug period summaries (contain wrong amounts due to two fee type bugs)
INSERT INTO report_summaries (client_id, period_date, total_held, total_committed, total_cancelled, total_fees, transaction_count) VALUES
    -- Alice, hold 3: percentage bug (fee=2.50 instead of 250.00)
    (1, '2024-01-20', 5000.00, 4997.50, 0.00,   2.50, 1),
    -- Bob, hold 4: tiered bug (fee=240.00 instead of 150.00)
    (2, '2024-02-01', 3000.00, 2760.00, 0.00, 240.00, 1),
    -- Charlie, hold 5: flat_rate was correct (fee=25.00)
    (3, '2024-02-15', 1500.00, 1475.00, 0.00,  25.00, 1),
    -- Alice, hold 6: percentage bug (fee=0.40 instead of 40.00)
    (1, '2024-02-20',  800.00,  799.60, 0.00,   0.40, 1);

-- Post bug period: cancelled hold summary
INSERT INTO report_summaries (client_id, period_date, total_held, total_committed, total_cancelled, total_fees, transaction_count) VALUES
    (3, '2024-03-13', 0.00, 0.00, 600.00, 0.00, 1);

-- -----------------------------------------------------------------------
-- client_fee_totals — cumulative fees per client (contains wrong totals due to bugs)
-- -----------------------------------------------------------------------
-- Alice Corp  (percentage bug): 100.00 (h1) + 2.50 (h3 wrong) + 0.40 (h6 wrong) = 102.90
--             correct should be: 100.00 + 250.00 + 40.00 = 390.00
-- Bob Ltd     (tiered bug):     200.00 (h2) + 240.00 (h4 wrong) = 440.00
--             correct should be: 200.00 + 150.00 = 350.00
-- Charlie Inc (flat_rate, no bug): 25.00 (h5 correct) = 25.00
INSERT INTO client_fee_totals (client_id, lifetime_fees, ytd_fees, last_updated) VALUES
    (1, 102.90, 102.90, '2024-02-20 17:05:00'),
    (2, 440.00, 440.00, '2024-02-01 11:05:00'),
    (3,  25.00,  25.00, '2024-02-15 15:05:00');

-- -----------------------------------------------------------------------
-- billing_ledger — immutable fee entries
-- -----------------------------------------------------------------------

-- Pre-bug holds (correct fee entries)
INSERT INTO billing_ledger (hold_id, client_id, entry_type, amount, notes, created_at) VALUES
    (1, 1, 'fee_charge', 100.00, 'Processing fee for hold 1', '2024-01-05 10:01:00'),
    (2, 2, 'fee_charge', 200.00, 'Processing fee for hold 2', '2024-01-10 14:01:00');

-- Bug-period holds — Alice & Bob have wrong amounts; Charlie is correct
INSERT INTO billing_ledger (hold_id, client_id, entry_type, amount, notes, created_at) VALUES
    (3, 1, 'fee_charge',   2.50, 'Processing fee for hold 3', '2024-01-20 09:01:00'),
    (4, 2, 'fee_charge', 240.00, 'Processing fee for hold 4', '2024-02-01 11:01:00'),
    (5, 3, 'fee_charge',  25.00, 'Processing fee for hold 5', '2024-02-15 15:01:00'),
    (6, 1, 'fee_charge',   0.40, 'Processing fee for hold 6', '2024-02-20 17:01:00');
