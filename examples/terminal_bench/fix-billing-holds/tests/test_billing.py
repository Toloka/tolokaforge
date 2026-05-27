"""
Outcome-verification tests for the fix-billing-holds task.

Assumes:
  - The agent has fixed fee calculation bugs in /app/fee_utils.py:
      1. percentage bug: calculate_fee_percentage was dividing by 100 extra
      2. tiered bug: TIER_SCHEDULE had thresholds 10x too small (100/1000 instead of 1000/10000)
  - The agent has run data migrations correcting:
      - Alice Corp (percentage): holds 3 and 6 committed 2024-01-15 to 2024-02-28
      - Bob Ltd (tiered): hold 4 committed 2024-01-15 to 2024-02-28
      - Charlie Inc (flat_rate): no migration needed — flat_rate was not buggy
  - The agent has rebuilt report_summaries with: cd /app && python aggregator.py
  - The billing service is running at http://localhost:8000
  - PostgreSQL is reachable at PG_HOST:5432 (env vars PG_HOST/PG_USER/PG_PASSWORD/PG_DB)
"""

import os

import psycopg2
import pytest
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DB = os.getenv("PG_DB", "billing_db")
PG_USER = os.getenv("PG_USER", "billing")
PG_PASSWORD = os.getenv("PG_PASSWORD", "billing")

BUG_PERIOD_START = "2024-01-15"
BUG_PERIOD_END = "2024-02-28"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_conn():
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
    )
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def api():
    """Return base URL after confirming service is up."""
    resp = requests.get(f"{BASE_URL}/health", timeout=10)
    assert resp.status_code == 200, "Billing service is not running"
    return BASE_URL


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------


def test_health_endpoint(api):
    resp = requests.get(f"{api}/health", timeout=10)
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


# ---------------------------------------------------------------------------
# 2. Bug is fixed — new operations calculate correctly per fee type
# ---------------------------------------------------------------------------


def test_percentage_fee_is_fixed(api):
    """After the fix, percentage-type clients must get fee = amount * fee_rate (not / 100 extra)."""
    # Alice Corp (client_id=1) is percentage type, fee_rate=0.05
    resp = requests.post(f"{api}/api/holds", json={"client_id": 1, "amount": 1000.0}, timeout=10)
    assert resp.status_code == 200, resp.text
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/commit", timeout=10)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Correct: fee = 1000 * 0.05 = 50.00
    assert abs(data["fee_charged"] - 50.0) < 0.01, (
        f"Percentage bug not fixed: fee_charged={data['fee_charged']}, expected 50.00 "
        f"(1000 * 0.05). If you got ~0.50 then the /100 division is still present."
    )
    assert abs(data["committed_amount"] - 950.0) < 0.01


def test_percentage_fee_large_amount(api):
    """Verify percentage fix with a larger amount (Alice Corp, $20000)."""
    resp = requests.post(f"{api}/api/holds", json={"client_id": 1, "amount": 20000.0}, timeout=10)
    assert resp.status_code == 200
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/commit", timeout=10)
    assert resp.status_code == 200
    data = resp.json()

    # fee = 20000 * 0.05 = 1000.00
    assert abs(data["fee_charged"] - 1000.0) < 0.01, (
        f"fee_charged={data['fee_charged']}, expected 1000.00"
    )
    assert abs(data["committed_amount"] - 19000.0) < 0.01


def test_tiered_fee_small_amount(api):
    """After tiered fix, amounts <= $1000 must use 3% tier."""
    # Bob Ltd (client_id=2) is tiered type
    resp = requests.post(f"{api}/api/holds", json={"client_id": 2, "amount": 500.0}, timeout=10)
    assert resp.status_code == 200
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/commit", timeout=10)
    assert resp.status_code == 200
    data = resp.json()

    # Correct: $500 <= $1000 → 3%: fee = 500 * 0.03 = 15.00
    assert abs(data["fee_charged"] - 15.0) < 0.01, (
        f"Tiered fee for $500 = {data['fee_charged']}, expected 15.00 "
        f"($500 <= $1000, 3% tier). Check TIER_SCHEDULE thresholds in fee_utils.py."
    )
    assert abs(data["committed_amount"] - 485.0) < 0.01


def test_tiered_fee_medium_amount(api):
    """After tiered fix, amounts $1001-$10000 must use 5% tier."""
    resp = requests.post(f"{api}/api/holds", json={"client_id": 2, "amount": 3000.0}, timeout=10)
    assert resp.status_code == 200
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/commit", timeout=10)
    assert resp.status_code == 200
    data = resp.json()

    # Correct: $3000 in $1001-$10000 → 5%: fee = 3000 * 0.05 = 150.00
    assert abs(data["fee_charged"] - 150.0) < 0.01, (
        f"Tiered fee for $3000 = {data['fee_charged']}, expected 150.00 "
        f"($1001-$10000 bracket = 5%). If 240.00 then 8% tier is still used (threshold bug)."
    )
    assert abs(data["committed_amount"] - 2850.0) < 0.01


def test_tiered_fee_large_amount(api):
    """After tiered fix, amounts over $10000 must use 8% tier."""
    resp = requests.post(f"{api}/api/holds", json={"client_id": 2, "amount": 15000.0}, timeout=10)
    assert resp.status_code == 200
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/commit", timeout=10)
    assert resp.status_code == 200
    data = resp.json()

    # Correct: $15000 > $10000 → 8%: fee = 15000 * 0.08 = 1200.00
    assert abs(data["fee_charged"] - 1200.0) < 0.01, (
        f"Tiered fee for $15000 = {data['fee_charged']}, expected 1200.00 (>$10000 = 8%)"
    )
    assert abs(data["committed_amount"] - 13800.0) < 0.01


def test_flat_rate_fee_correct(api):
    """Flat-rate clients always pay the fixed fee ($25) regardless of hold amount."""
    # Charlie Inc (client_id=3) is flat_rate type, flat fee = $25
    resp = requests.post(f"{api}/api/holds", json={"client_id": 3, "amount": 5000.0}, timeout=10)
    assert resp.status_code == 200
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/commit", timeout=10)
    assert resp.status_code == 200
    data = resp.json()

    # Flat rate: fee = $25.00 regardless of amount
    assert abs(data["fee_charged"] - 25.0) < 0.01, (
        f"Flat-rate fee for $5000 = {data['fee_charged']}, expected 25.00 (fixed flat fee)"
    )
    assert abs(data["committed_amount"] - 4975.0) < 0.01


# ---------------------------------------------------------------------------
# 3. Historical data corrected (bug period: 2024-01-15 to 2024-02-28)
#    Only percentage (Alice) and tiered (Bob) holds were affected.
#    flat_rate (Charlie) hold 5 was already correct.
# ---------------------------------------------------------------------------


def test_hold3_fee_corrected(db_conn):
    """Hold 3 (Alice Corp, percentage, $5000, committed 2024-01-20): fee must be 250.00."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT fee_charged, committed_amount FROM holds WHERE id = 3")
        row = cur.fetchone()
    assert row is not None
    fee_charged, committed_amount = float(row[0]), float(row[1])
    assert abs(fee_charged - 250.0) < 0.01, (
        f"Hold 3 fee_charged={fee_charged}, expected 250.00 (5000 * 0.05). "
        f"This is a percentage-type hold — fix calculate_fee_percentage."
    )
    assert abs(committed_amount - 4750.0) < 0.01


def test_hold4_fee_corrected(db_conn):
    """Hold 4 (Bob Ltd, tiered, $3000, committed 2024-02-01): fee must be 150.00."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT fee_charged, committed_amount FROM holds WHERE id = 4")
        row = cur.fetchone()
    assert row is not None
    fee_charged, committed_amount = float(row[0]), float(row[1])
    assert abs(fee_charged - 150.0) < 0.01, (
        f"Hold 4 fee_charged={fee_charged}, expected 150.00 ($3000 at 5% tier). "
        f"If 240.00, the tiered TIER_SCHEDULE thresholds have not been fixed."
    )
    assert abs(committed_amount - 2850.0) < 0.01


def test_hold5_fee_unchanged(db_conn):
    """Hold 5 (Charlie Inc, flat_rate, $1500): fee must be 25.00 (was never wrong)."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT fee_charged, committed_amount FROM holds WHERE id = 5")
        row = cur.fetchone()
    assert row is not None
    fee_charged, committed_amount = float(row[0]), float(row[1])
    assert abs(fee_charged - 25.0) < 0.01, (
        f"Hold 5 fee_charged={fee_charged}, expected 25.00 (flat $25 fee, no bug here)"
    )
    assert abs(committed_amount - 1475.0) < 0.01


def test_hold6_fee_corrected(db_conn):
    """Hold 6 (Alice Corp, percentage, $800, committed 2024-02-20): fee must be 40.00."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT fee_charged, committed_amount FROM holds WHERE id = 6")
        row = cur.fetchone()
    assert row is not None
    fee_charged, committed_amount = float(row[0]), float(row[1])
    assert abs(fee_charged - 40.0) < 0.01, (
        f"Hold 6 fee_charged={fee_charged}, expected 40.00 (800 * 0.05)"
    )
    assert abs(committed_amount - 760.0) < 0.01


def test_pre_bug_holds_unchanged(db_conn):
    """Holds before bug period must retain their original (correct) values."""
    with db_conn.cursor() as cur:
        # Hold 1: Alice Corp (percentage), $2000, fee=100.00
        cur.execute("SELECT fee_charged, committed_amount FROM holds WHERE id = 1")
        row = cur.fetchone()
    assert abs(float(row[0]) - 100.0) < 0.01
    assert abs(float(row[1]) - 1900.0) < 0.01

    with db_conn.cursor() as cur:
        # Hold 2: Bob Ltd (tiered), $4000, fee=200.00
        cur.execute("SELECT fee_charged, committed_amount FROM holds WHERE id = 2")
        row = cur.fetchone()
    assert abs(float(row[0]) - 200.0) < 0.01
    assert abs(float(row[1]) - 3800.0) < 0.01


# ---------------------------------------------------------------------------
# 4. Transactions table corrected
# ---------------------------------------------------------------------------


def test_transactions_corrected_for_hold3(db_conn):
    """Commit transaction for hold 3 must reflect corrected committed_amount=4750.00."""
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT amount FROM transactions WHERE hold_id = 3 AND transaction_type = 'commit'"
        )
        row = cur.fetchone()
    assert row is not None, "No commit transaction for hold 3"
    assert abs(float(row[0]) - 4750.0) < 0.01, (
        f"Transaction amount for hold 3 commit = {row[0]}, expected 4750.00"
    )


def test_transactions_corrected_for_hold4(db_conn):
    """Commit transaction for hold 4 must reflect corrected committed_amount=2850.00."""
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT amount FROM transactions WHERE hold_id = 4 AND transaction_type = 'commit'"
        )
        row = cur.fetchone()
    assert row is not None
    assert abs(float(row[0]) - 2850.0) < 0.01, (
        f"Transaction amount for hold 4 commit = {row[0]}, expected 2850.00"
    )


def test_transactions_unchanged_for_hold5(db_conn):
    """Commit transaction for hold 5 must remain 1475.00 (flat_rate, no bug)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT amount FROM transactions WHERE hold_id = 5 AND transaction_type = 'commit'"
        )
        row = cur.fetchone()
    assert row is not None
    assert abs(float(row[0]) - 1475.0) < 0.01, (
        f"Transaction amount for hold 5 commit = {row[0]}, expected 1475.00"
    )


def test_transactions_corrected_for_hold6(db_conn):
    """Commit transaction for hold 6 must reflect corrected committed_amount=760.00."""
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT amount FROM transactions WHERE hold_id = 6 AND transaction_type = 'commit'"
        )
        row = cur.fetchone()
    assert row is not None
    assert abs(float(row[0]) - 760.0) < 0.01, (
        f"Transaction amount for hold 6 commit = {row[0]}, expected 760.00"
    )


# ---------------------------------------------------------------------------
# 5. Reports API and report_summaries consistency
# ---------------------------------------------------------------------------


def test_reports_api_accessible(api):
    """GET /api/reports must return 200 and a list."""
    resp = requests.get(f"{api}/api/reports", timeout=10)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_reports_bug_period_fees_corrected(api):
    """
    Total fees in bug period from reports API must equal corrected sum:
    Hold 3 (Alice, percentage): 250.00
    Hold 4 (Bob, tiered):       150.00
    Hold 5 (Charlie, flat_rate): 25.00  ← was never wrong
    Hold 6 (Alice, percentage):  40.00
    Total: 465.00
    """
    resp = requests.get(f"{api}/api/reports", timeout=10)
    assert resp.status_code == 200
    summaries = resp.json()

    bug_period_fees = sum(
        s["total_fees"]
        for s in summaries
        if BUG_PERIOD_START <= s["period_date"] <= BUG_PERIOD_END
    )
    expected = 465.0  # 250 + 150 + 25 + 40
    assert abs(bug_period_fees - expected) < 0.01, (
        f"Bug period total_fees={bug_period_fees}, expected {expected}. "
        "Did you run the aggregator after migration? Note: flat_rate (Charlie) was not buggy."
    )


def test_reports_alice_corp_total_fees(api):
    """
    Alice Corp (client_id=1) total fees across all periods:
    Before bug: 100.00 (hold 1)
    Bug period: 250.00 (hold 3) + 40.00 (hold 6) = 290.00
    Total: 390.00
    """
    resp = requests.get(f"{api}/api/reports?client_id=1", timeout=10)
    assert resp.status_code == 200
    summaries = resp.json()

    total_fees = sum(s["total_fees"] for s in summaries)
    expected = 390.0
    assert abs(total_fees - expected) < 0.01, (
        f"Alice Corp total fees={total_fees}, expected {expected}"
    )


def test_report_summaries_match_holds(db_conn):
    """
    The sum of total_fees in report_summaries must match the sum of fee_charged
    in the holds table for the seeded holds (committed before 2024-04-01).
    Excludes holds created by tests during this run.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT SUM(fee_charged) FROM holds "
            "WHERE status = 'committed' AND committed_at < '2024-04-01'"
        )
        holds_total = float(cur.fetchone()[0])

        cur.execute(
            "SELECT SUM(total_fees) FROM report_summaries "
            "WHERE period_date < '2024-04-01'"
        )
        reports_total = float(cur.fetchone()[0])

    assert abs(holds_total - reports_total) < 0.01, (
        f"report_summaries.total_fees ({reports_total}) does not match "
        f"holds.fee_charged sum ({holds_total}) for seeded holds. "
        "Run 'cd /app && python aggregator.py' after migration."
    )


# ---------------------------------------------------------------------------
# 6. client_fee_totals corrected
# ---------------------------------------------------------------------------


def test_client_fee_totals_alice(db_conn):
    """
    Alice Corp (client_id=1) lifetime_fees must equal corrected sum:
    Hold 1 (percentage, correct): 100.00
    Hold 3 (percentage, corrected): 250.00
    Hold 6 (percentage, corrected): 40.00
    Total: 390.00
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT lifetime_fees FROM client_fee_totals WHERE client_id = 1")
        row = cur.fetchone()
    assert row is not None, "No client_fee_totals row for Alice Corp (client_id=1)"
    assert abs(float(row[0]) - 390.0) < 0.01, (
        f"Alice Corp lifetime_fees={row[0]}, expected 390.00. "
        "Did you run 'python aggregator.py' after migration?"
    )


def test_client_fee_totals_bob(db_conn):
    """
    Bob Ltd (client_id=2) lifetime_fees must equal corrected sum:
    Hold 2 (tiered, correct): 200.00
    Hold 4 (tiered, corrected): 150.00
    Total: 350.00
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT lifetime_fees FROM client_fee_totals WHERE client_id = 2")
        row = cur.fetchone()
    assert row is not None, "No client_fee_totals row for Bob Ltd (client_id=2)"
    assert abs(float(row[0]) - 350.0) < 0.01, (
        f"Bob Ltd lifetime_fees={row[0]}, expected 350.00. "
        "Hold 4 tiered fee should be 150.00 (5% tier), not 240.00 (buggy 8% tier)."
    )


def test_client_fee_totals_charlie(db_conn):
    """
    Charlie Inc (client_id=3) lifetime_fees must equal:
    Hold 5 (flat_rate, was always correct): 25.00
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT lifetime_fees FROM client_fee_totals WHERE client_id = 3")
        row = cur.fetchone()
    assert row is not None, "No client_fee_totals row for Charlie Inc (client_id=3)"
    assert abs(float(row[0]) - 25.0) < 0.01, (
        f"Charlie Inc lifetime_fees={row[0]}, expected 25.00 (flat $25 fee, no bug)"
    )


# ---------------------------------------------------------------------------
# 7. billing_ledger corrected (adjustment entries inserted for buggy holds)
# ---------------------------------------------------------------------------


def test_billing_ledger_hold3_net_fee(db_conn):
    """
    billing_ledger net fee for hold 3 must be 250.00 after adjustment.
    Original entry: 2.50 (percentage bug). Adjustment: +247.50. Net: 250.00.
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT SUM(amount) FROM billing_ledger WHERE hold_id = 3")
        row = cur.fetchone()
    assert row[0] is not None, "No billing_ledger entries for hold 3"
    net = float(row[0])
    assert abs(net - 250.0) < 0.01, (
        f"billing_ledger net for hold 3 = {net}, expected 250.00. "
        "Insert a fee_adjustment entry: 250.00 - 2.50 = +247.50"
    )


def test_billing_ledger_hold4_net_fee(db_conn):
    """
    billing_ledger net fee for hold 4 must be 150.00 after adjustment.
    Original entry: 240.00 (tiered bug over-charged). Adjustment: -90.00. Net: 150.00.
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT SUM(amount) FROM billing_ledger WHERE hold_id = 4")
        row = cur.fetchone()
    assert row[0] is not None
    net = float(row[0])
    assert abs(net - 150.0) < 0.01, (
        f"billing_ledger net for hold 4 = {net}, expected 150.00. "
        "Insert a fee_adjustment entry: 150.00 - 240.00 = -90.00 "
        "(Bob was over-charged by the tiered bug — adjustment is negative)"
    )


def test_billing_ledger_hold5_net_fee(db_conn):
    """billing_ledger net fee for hold 5 must remain 25.00 (flat_rate, no bug)."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT SUM(amount) FROM billing_ledger WHERE hold_id = 5")
        row = cur.fetchone()
    assert row[0] is not None
    assert abs(float(row[0]) - 25.0) < 0.01, (
        f"billing_ledger net for hold 5 = {row[0]}, expected 25.00 (flat_rate, no adjustment needed)"
    )


def test_billing_ledger_hold6_net_fee(db_conn):
    """
    billing_ledger net fee for hold 6 must be 40.00 after adjustment.
    Original entry: 0.40 (percentage bug). Adjustment: +39.60. Net: 40.00.
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT SUM(amount) FROM billing_ledger WHERE hold_id = 6")
        row = cur.fetchone()
    assert row[0] is not None
    assert abs(float(row[0]) - 40.0) < 0.01, (
        f"billing_ledger net for hold 6 = {row[0]}, expected 40.00"
    )


def test_billing_ledger_pre_bug_unchanged(db_conn):
    """Pre-bug holds (1, 2) must have only their original fee_charge entries unchanged."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT SUM(amount) FROM billing_ledger WHERE hold_id = 1")
        h1 = float(cur.fetchone()[0])
        cur.execute("SELECT SUM(amount) FROM billing_ledger WHERE hold_id = 2")
        h2 = float(cur.fetchone()[0])
    assert abs(h1 - 100.0) < 0.01, f"Hold 1 ledger net = {h1}, expected 100.00"
    assert abs(h2 - 200.0) < 0.01, f"Hold 2 ledger net = {h2}, expected 200.00"


# ---------------------------------------------------------------------------
# 8. Cancel and get hold API
# ---------------------------------------------------------------------------


def test_cancel_hold(api):
    """POST /api/holds/{id}/cancel must cancel a pending hold."""
    resp = requests.post(f"{api}/api/holds", json={"client_id": 3, "amount": 500.0}, timeout=10)
    assert resp.status_code == 200
    hold_id = resp.json()["id"]

    resp = requests.post(f"{api}/api/holds/{hold_id}/cancel", timeout=10)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_get_hold(api):
    """GET /api/holds/{id} must return hold details."""
    resp = requests.get(f"{api}/api/holds/1", timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["client_id"] == 1
    assert "amount" in data
    assert "status" in data
