#!/usr/bin/env python3
"""Airline passenger segmentation pipeline.

Reads booking and flight data from PostgreSQL, computes RFM features
(Recency, Frequency, Monetary), runs K-means clustering, and writes
segment assignments back to reporting tables.
"""
import sys
from datetime import datetime

from extract import extract_bookings, extract_flights, extract_passengers
from features import compute_rfm_features
from cluster import run_clustering
from report import (write_rfm_features, write_segments,
                    write_segment_report, log_pipeline_step)


def main():
    print("=" * 60)
    print("Airline Passenger Segmentation Pipeline")
    print(f"Started at: {datetime.now()}")
    print("=" * 60)

    try:
        # Step 1: Extract data
        print("\n[1/4] Extracting data from PostgreSQL...")
        log_pipeline_step('extract', 'running', 'Starting data extraction')

        bookings = extract_bookings()
        flights = extract_flights()
        passengers = extract_passengers()

        print(f"  Loaded {len(bookings)} bookings, {len(flights)} flights, "
              f"{len(passengers)} passengers")
        log_pipeline_step('extract', 'completed',
                          f'Extracted {len(bookings)} bookings, {len(flights)} flights')

        # Step 2: Compute RFM features
        print("\n[2/4] Computing RFM features...")
        log_pipeline_step('features', 'running', 'Computing RFM features')

        rfm = compute_rfm_features(bookings, flights)
        log_pipeline_step('features', 'completed',
                          f'Computed features for {len(rfm)} passengers')

        # Step 3: Run clustering
        print("\n[3/4] Running K-means clustering...")
        log_pipeline_step('clustering', 'running', 'Starting K-means clustering')

        rfm_segmented, model = run_clustering(rfm)
        log_pipeline_step('clustering', 'completed',
                          f'Assigned {len(rfm_segmented)} passengers to segments')

        # Step 4: Write results
        print("\n[4/4] Writing results to database...")
        log_pipeline_step('reporting', 'running', 'Writing results')

        write_rfm_features(rfm_segmented)
        write_segments(rfm_segmented)
        write_segment_report(rfm_segmented)

        log_pipeline_step('reporting', 'completed', 'All results written successfully')

        print("\n" + "=" * 60)
        print("Segmentation completed successfully")
        print("=" * 60)

    except Exception as e:
        log_pipeline_step('error', 'failed', str(e))
        print(f"\nPipeline failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
