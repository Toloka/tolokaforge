# Task: Fix Billing Holds

You are investigating anomalies in a billing microservice that manages
payment holds with a hold/commit/cancel flow. Clients have been reporting
incorrect fee charges — some are being charged far too little, while others
may be overcharged. The issue appears to have started sometime in early 2024,
but you need to investigate the data to identify the exact scope.

## Stack

- Python 3.11 FastAPI service
- PostgreSQL 15 (primary data store)

## Code Layout

Code lives in `/app`:
- `/app/main.py` — FastAPI entry point
- `/app/database.py` — SQLAlchemy engine and session
- `/app/models.py` — SQLAlchemy ORM models
- `/app/config.py` — configuration
- `/app/fee_utils.py` — fee calculation functions (one per fee type)
- `/app/routers/holds.py` — holds API (create, commit, cancel)
- `/app/routers/reports.py` — reports API
- `/app/aggregator.py` — rebuilds report_summaries from holds table

Service is already running at: http://localhost:8000

## Database Connection

| Field    | Value       |
|----------|-------------|
| Host     | localhost (env: PG_HOST) |
| Port     | 5432        |
| Name     | billing_db  |
| User     | billing     |
| Password | billing     |

## Database Schema

```
clients (id, name, email, balance, fee_type)
holds (id, client_id, amount, fee_rate, fee_charged, committed_amount,
       status, created_at, committed_at, cancelled_at)
transactions (id, hold_id, client_id, transaction_type, amount, created_at)
platform_settings (key, value)
report_summaries (id, client_id, period_date, total_held, total_committed,
                  total_cancelled, total_fees, transaction_count)
client_fee_totals (client_id, lifetime_fees, ytd_fees, last_updated)
billing_ledger (id, hold_id, client_id, entry_type, amount, notes, created_at)
```

> Use `\dt` in psql to confirm the full list of tables before you start.

## Fee Type System

Each client is assigned a `fee_type` in the `clients` table. The billing service
supports three fee types, each with its own calculation logic in `fee_utils.py`:

| fee_type    | Description |
|-------------|-------------|
| `percentage` | Fee is a percentage of the hold amount |
| `tiered`     | Fee depends on amount brackets (defined in `TIER_SCHEDULE` in `fee_utils.py`) |
| `flat_rate`  | Fee is a fixed dollar amount per transaction |

## API Endpoints

```
POST /api/holds              — create a hold: {"client_id": 1, "amount": 100.0}
POST /api/holds/{id}/commit  — commit a hold (finalizes charge + fee)
POST /api/holds/{id}/cancel  — cancel a hold (releases money)
GET  /api/holds/{id}         — get hold details
GET  /api/reports            — aggregated reports (optional ?client_id=N)
GET  /health                 — health check
```

## Architecture Notes

- When a hold is committed, the platform charges a processing fee based on the
  client's `fee_type`. The fee calculation logic lives in `/app/fee_utils.py`.
- `fee_rate` semantics depend on `fee_type`: for `flat_rate` clients it stores
  the flat fee dollar amount directly; for `tiered` clients it is unused (stored
  as 0). Platform `fee_rate` conventions are documented in
  `/app/legacy_fee_config.json`.
- The reports API reads from pre-aggregated `report_summaries`, NOT from raw
  holds/transactions directly.
- The `aggregator.py` script rebuilds `report_summaries` and `client_fee_totals`
  from the holds table. Run it with: `cd /app && python aggregator.py`
- `billing_ledger` is an **append-only** audit log for regulatory compliance.
  PostgreSQL triggers block UPDATE/DELETE on it. Fee corrections must be
  recorded by **INSERT**ing new rows (e.g. `entry_type='fee_adjustment'`),
  never by modifying existing entries.
- Historical hold data from before the bugs were introduced is correct. Holds
  committed prior to the bug period must NOT be altered — identify the
  bug-period boundary from the data before migrating anything.

## The Problem

Clients have been reporting incorrect billing amounts since early 2024. Some
clients are seeing fees far lower than expected; at least one client may have
been overcharged. The bugs affect different fee types differently.

Both primary data AND pre-aggregated report tables may be corrupted for the
affected time period.

**Important:** not all fee types are buggy. Identify which fee types have bugs
and which don't — and only migrate data for the affected clients.

## Your Tasks

1. **Investigate** the code and data to identify which fee types have bugs and
   what the correct calculation should be for each
2. **Fix all bugs** in `/app/fee_utils.py`
3. **Restart the service:**
   ```bash
   pkill uvicorn || true
   cd /app && uvicorn main:app --host 0.0.0.0 --port 8000 &
   sleep 2
   ```
4. **Run targeted data migrations** for each affected fee type.
   Do NOT blindly migrate all holds — identify which clients/fee_types were
   affected and apply the correct formula per type. Every table that stores
   fee-derived data must be corrected:
   - `holds` — correct `fee_charged` and `committed_amount` for affected holds
   - `transactions` — correct the `commit` row's `amount` (= new `committed_amount`)
   - `billing_ledger` — INSERT adjustment rows (append-only; UPDATE is blocked).
     An adjustment amount can be negative if a client was over-charged.
   Pre-bug holds are already correct — leave them alone.
5. **Rebuild all aggregated tables:**
   ```bash
   cd /app && python aggregator.py
   ```
   This rebuilds `report_summaries` and `client_fee_totals` from `holds`.
