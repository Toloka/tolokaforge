"""Rebuilds report_summaries from the holds table.

Run with: cd /app && python aggregator.py
"""
import sys

sys.path.insert(0, "/app")

from sqlalchemy import text

from database import SessionLocal
from models import ReportSummary


def run():
    db = SessionLocal()
    try:
        # Clear existing summaries
        db.execute(text("DELETE FROM report_summaries"))
        db.commit()

        # Aggregate committed holds by client and calendar day
        committed = db.execute(
            text("""
                SELECT
                    client_id,
                    DATE(committed_at)::text AS period_date,
                    SUM(amount)           AS total_held,
                    SUM(committed_amount) AS total_committed,
                    SUM(fee_charged)      AS total_fees,
                    COUNT(*)              AS transaction_count
                FROM holds
                WHERE status = 'committed'
                GROUP BY client_id, DATE(committed_at)
            """)
        ).fetchall()

        for row in committed:
            db.add(
                ReportSummary(
                    client_id=row.client_id,
                    period_date=row.period_date,
                    total_held=row.total_held,
                    total_committed=row.total_committed,
                    total_cancelled=0,
                    total_fees=row.total_fees,
                    transaction_count=row.transaction_count,
                )
            )

        # Aggregate cancelled holds by client and calendar day
        cancelled = db.execute(
            text("""
                SELECT
                    client_id,
                    DATE(cancelled_at)::text AS period_date,
                    SUM(amount)              AS total_cancelled,
                    COUNT(*)                 AS transaction_count
                FROM holds
                WHERE status = 'cancelled'
                GROUP BY client_id, DATE(cancelled_at)
            """)
        ).fetchall()

        db.flush()

        for row in cancelled:
            existing = (
                db.query(ReportSummary)
                .filter(
                    ReportSummary.client_id == row.client_id,
                    ReportSummary.period_date == row.period_date,
                )
                .first()
            )
            if existing:
                existing.total_cancelled = float(existing.total_cancelled or 0) + float(
                    row.total_cancelled
                )
                existing.transaction_count += row.transaction_count
            else:
                db.add(
                    ReportSummary(
                        client_id=row.client_id,
                        period_date=row.period_date,
                        total_held=0,
                        total_committed=0,
                        total_cancelled=row.total_cancelled,
                        total_fees=0,
                        transaction_count=row.transaction_count,
                    )
                )

        db.commit()

        total = db.query(ReportSummary).count()
        print(f"report_summaries rebuilt successfully. Total rows: {total}")

        # Rebuild client_fee_totals from committed holds
        db.execute(text("DELETE FROM client_fee_totals"))
        db.execute(text("""
            INSERT INTO client_fee_totals (client_id, lifetime_fees, ytd_fees, last_updated)
            SELECT
                client_id,
                SUM(fee_charged)  AS lifetime_fees,
                SUM(fee_charged)  AS ytd_fees,
                MAX(committed_at) AS last_updated
            FROM holds
            WHERE status = 'committed'
            GROUP BY client_id
        """))
        db.commit()
        print("client_fee_totals rebuilt successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
