"""Reporting module — writes segmentation results to PostgreSQL."""
import psycopg2
from datetime import datetime
from config import DB_CONFIG


def write_rfm_features(rfm_df):
    """Write RFM features to the rfm_features table."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE rfm_features")

    now = datetime.now()
    for _, row in rfm_df.iterrows():
        cur.execute(
            "INSERT INTO rfm_features (passenger_id, recency, frequency, monetary, computed_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (int(row['passenger_id']), float(row['recency']),
             int(row['frequency']), float(row['monetary']), now)
        )

    conn.commit()
    cur.close()
    conn.close()
    print(f"  Wrote {len(rfm_df)} RFM feature records")


def write_segments(rfm_df):
    """Write segment assignments to the segments table."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE segments")

    now = datetime.now()
    for _, row in rfm_df.iterrows():
        cur.execute(
            "INSERT INTO segments (passenger_id, segment_id, segment_name, assigned_at) "
            "VALUES (%s, %s, %s, %s)",
            (int(row['passenger_id']), int(row['segment_id']),
             row['segment_name'], now)
        )

    conn.commit()
    cur.close()
    conn.close()
    print(f"  Wrote {len(rfm_df)} segment assignments")


def write_segment_report(rfm_df):
    """Write segment summary report."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("TRUNCATE TABLE segment_report")

    now = datetime.now()
    summary = rfm_df.groupby(['segment_id', 'segment_name']).agg(
        passenger_count=('passenger_id', 'count'),
        avg_recency=('recency', 'mean'),
        avg_frequency=('frequency', 'mean'),
        avg_monetary=('monetary', 'mean'),
    ).reset_index()

    for _, row in summary.iterrows():
        cur.execute(
            "INSERT INTO segment_report (segment_id, segment_name, passenger_count, "
            "avg_recency, avg_frequency, avg_monetary, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (int(row['segment_id']), row['segment_name'],
             int(row['passenger_count']), float(row['avg_recency']),
             float(row['avg_frequency']), float(row['avg_monetary']), now)
        )

    conn.commit()
    cur.close()
    conn.close()
    print(f"  Wrote {len(summary)} segment report records")


def log_pipeline_step(step, status, message):
    """Log a pipeline step to the pipeline_log table."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO pipeline_log (step, status, message, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (step, status, message, datetime.now())
    )

    conn.commit()
    cur.close()
    conn.close()
