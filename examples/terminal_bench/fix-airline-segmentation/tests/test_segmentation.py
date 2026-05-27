"""Outcome-driven tests for the airline passenger segmentation pipeline.

These tests verify the final state of the database after the pipeline has
been run, checking that output tables contain correct values and that the
segmentation results are internally consistent.
"""
import os
import pytest
import psycopg2


DB_CONFIG = {
    'host': os.getenv('PG_HOST', 'localhost'),
    'port': int(os.getenv('PG_PORT', '5432')),
    'dbname': os.getenv('PG_DB', 'airline_db'),
    'user': os.getenv('PG_USER', 'airline'),
    'password': os.getenv('PG_PASSWORD', 'seg_pipeline_2024'),
}


@pytest.fixture(scope="session")
def db():
    """Connect to the airline database."""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Recency feature validation
# ---------------------------------------------------------------------------

class TestRecency:
    def test_recency_non_negative(self, db):
        """All recency values must be >= 0."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM rfm_features WHERE recency < 0")
        negative_count = cur.fetchone()[0]
        cur.close()
        assert negative_count == 0, (
            f"Found {negative_count} passengers with negative recency."
        )

    def test_recency_values_consistent(self, db):
        """Recency values should be consistent with source data."""
        cur = db.cursor()
        cur.execute("""
            SELECT r.passenger_id, r.recency,
                   ('2024-12-01'::date - MAX(b.booking_date)) AS expected_recency
              FROM rfm_features r
              JOIN bookings b ON b.passenger_id = r.passenger_id
             WHERE b.status != 'cancelled'
             GROUP BY r.passenger_id, r.recency
            HAVING ABS(r.recency - ('2024-12-01'::date - MAX(b.booking_date))) > 1
        """)
        mismatches = cur.fetchall()
        cur.close()
        assert len(mismatches) == 0, (
            f"Found {len(mismatches)} passengers with inconsistent recency values."
        )


# ---------------------------------------------------------------------------
# Frequency feature validation
# ---------------------------------------------------------------------------

class TestFrequency:
    def test_frequency_values_correct(self, db):
        """Frequency values should match expected counts from source data."""
        cur = db.cursor()
        cur.execute("""
            SELECT r.passenger_id, r.frequency,
                   COUNT(b.id) AS expected_frequency
              FROM rfm_features r
              JOIN bookings b ON b.passenger_id = r.passenger_id
                             AND b.status != 'cancelled'
             GROUP BY r.passenger_id, r.frequency
            HAVING r.frequency != COUNT(b.id)
        """)
        mismatches = cur.fetchall()
        cur.close()
        assert len(mismatches) == 0, (
            f"Found {len(mismatches)} passengers with incorrect frequency values."
        )

    def test_frequency_mean_consistent(self, db):
        """Average frequency should be consistent with expected aggregate."""
        cur = db.cursor()
        cur.execute("SELECT AVG(frequency) FROM rfm_features")
        avg_frequency = float(cur.fetchone()[0])

        cur.execute("""
            SELECT AVG(cnt) FROM (
                SELECT passenger_id, COUNT(*) AS cnt
                  FROM bookings
                 WHERE status != 'cancelled'
                 GROUP BY passenger_id
            ) t
        """)
        expected_avg = float(cur.fetchone()[0])
        cur.close()

        assert abs(avg_frequency - expected_avg) < 0.5, (
            f"Average frequency {avg_frequency:.2f} differs from expected "
            f"{expected_avg:.2f}."
        )


# ---------------------------------------------------------------------------
# Monetary feature validation
# ---------------------------------------------------------------------------

class TestMonetary:
    def test_monetary_values_correct(self, db):
        """Monetary values should match expected totals from source data."""
        cur = db.cursor()
        cur.execute("""
            SELECT r.passenger_id, r.monetary,
                   SUM(b.total_price) AS expected_monetary
              FROM rfm_features r
              JOIN bookings b ON b.passenger_id = r.passenger_id
                             AND b.status != 'cancelled'
             GROUP BY r.passenger_id, r.monetary
            HAVING ABS(r.monetary - SUM(b.total_price)) > 1.0
        """)
        mismatches = cur.fetchall()
        cur.close()
        assert len(mismatches) == 0, (
            f"Found {len(mismatches)} passengers with incorrect monetary values."
        )

    def test_monetary_reasonable_range(self, db):
        """Monetary values should fall within a reasonable range."""
        cur = db.cursor()
        cur.execute("""
            SELECT MAX(total) FROM (
                SELECT SUM(total_price) AS total
                  FROM bookings
                 WHERE status != 'cancelled'
                 GROUP BY passenger_id
            ) t
        """)
        max_booking_total = float(cur.fetchone()[0])

        cur.execute("SELECT MAX(monetary) FROM rfm_features")
        max_monetary = float(cur.fetchone()[0])
        cur.close()

        assert max_monetary <= max_booking_total * 1.01, (
            f"Max monetary {max_monetary:.2f} exceeds expected ceiling "
            f"{max_booking_total:.2f}."
        )


# ---------------------------------------------------------------------------
# Clustering quality validation
# ---------------------------------------------------------------------------

class TestClustering:
    def test_cluster_size_distribution(self, db):
        """Each cluster must have at least 5% of total passengers."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM segments")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT segment_id, COUNT(*) AS cnt
              FROM segments
             GROUP BY segment_id
        """)
        clusters = cur.fetchall()
        cur.close()

        threshold = total * 0.05
        for seg_id, cnt in clusters:
            assert cnt >= threshold, (
                f"Segment {seg_id} has only {cnt} passengers ({cnt/total*100:.1f}%). "
                f"Minimum is 5%."
            )

    def test_cluster_count(self, db):
        """There should be exactly 5 clusters."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(DISTINCT segment_id) FROM segments")
        n_clusters = cur.fetchone()[0]
        cur.close()
        assert n_clusters == 5, (
            f"Expected 5 clusters, found {n_clusters}."
        )

    def test_cluster_differentiation(self, db):
        """Cluster centroids should show meaningful variation across all features."""
        cur = db.cursor()
        cur.execute("""
            SELECT segment_id,
                   AVG(r.recency) AS avg_r,
                   AVG(r.frequency) AS avg_f,
                   AVG(r.monetary) AS avg_m
              FROM segments s
              JOIN rfm_features r ON r.passenger_id = s.passenger_id
             GROUP BY segment_id
             ORDER BY segment_id
        """)
        centroids = cur.fetchall()
        cur.close()

        recency_values = [c[1] for c in centroids]
        frequency_values = [c[2] for c in centroids]

        recency_range = float(max(recency_values) - min(recency_values))
        frequency_range = float(max(frequency_values) - min(frequency_values))
        recency_mean = float(sum(recency_values)) / len(recency_values)
        frequency_mean = float(sum(frequency_values)) / len(frequency_values)

        recency_cv = recency_range / max(recency_mean, 1)
        frequency_cv = frequency_range / max(frequency_mean, 1)

        assert recency_cv > 0.10, (
            f"Recency variation across clusters is too low (CV={recency_cv:.3f})."
        )
        assert frequency_cv > 0.10, (
            f"Frequency variation across clusters is too low (CV={frequency_cv:.3f})."
        )


# ---------------------------------------------------------------------------
# Output table integrity
# ---------------------------------------------------------------------------

class TestOutputTables:
    def test_rfm_features_populated(self, db):
        """rfm_features table should have one row per passenger."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM rfm_features")
        rfm_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM passengers")
        passenger_count = cur.fetchone()[0]
        cur.close()
        assert rfm_count == passenger_count, (
            f"rfm_features has {rfm_count} rows but there are "
            f"{passenger_count} passengers."
        )

    def test_segments_populated(self, db):
        """segments table should have one row per passenger."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM segments")
        seg_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM passengers")
        passenger_count = cur.fetchone()[0]
        cur.close()
        assert seg_count == passenger_count, (
            f"segments has {seg_count} rows but there are "
            f"{passenger_count} passengers."
        )

    def test_segment_report_populated(self, db):
        """segment_report should have exactly 5 rows (one per segment)."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM segment_report")
        report_count = cur.fetchone()[0]
        cur.close()
        assert report_count == 5, (
            f"segment_report has {report_count} rows, expected 5."
        )

    def test_pipeline_log_has_entries(self, db):
        """pipeline_log should have recent entries."""
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM pipeline_log")
        log_count = cur.fetchone()[0]
        cur.close()
        assert log_count > 0, "pipeline_log is empty."

    def test_segment_report_recency_non_negative(self, db):
        """Segment report average recency should be non-negative for all segments."""
        cur = db.cursor()
        cur.execute(
            "SELECT segment_name, avg_recency FROM segment_report "
            "WHERE avg_recency < 0"
        )
        negative_segments = cur.fetchall()
        cur.close()
        assert len(negative_segments) == 0, (
            f"Found {len(negative_segments)} segments with negative avg_recency: "
            f"{[s[0] for s in negative_segments]}."
        )


# ---------------------------------------------------------------------------
# Cross-table consistency
# ---------------------------------------------------------------------------

class TestCrossTableConsistency:
    def test_rfm_recency_mean_reasonable(self, db):
        """Average recency across all passengers should fall within a reasonable range."""
        cur = db.cursor()
        cur.execute("SELECT AVG(recency) FROM rfm_features")
        avg_recency = float(cur.fetchone()[0])
        cur.close()
        assert 30 <= avg_recency <= 400, (
            f"Average recency is {avg_recency:.1f}, expected in [30, 400]."
        )
