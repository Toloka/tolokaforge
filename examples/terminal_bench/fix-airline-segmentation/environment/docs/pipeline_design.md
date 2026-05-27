# Passenger Segmentation Pipeline — Design Document

## Overview

This pipeline segments airline passengers into behavioral groups using RFM
(Recency, Frequency, Monetary) analysis. It reads booking and flight data from
PostgreSQL, computes per-passenger RFM features, applies K-means clustering
(k=5), and writes the segment assignments to reporting tables.

## Architecture

```
  PostgreSQL (source)         Pipeline (Python)           PostgreSQL (output)
 ┌───────────────────┐     ┌──────────────────────┐     ┌───────────────────┐
 │ passengers        │────▶│ 1. extract.py        │     │ rfm_features      │
 │ bookings          │────▶│ 2. features.py (RFM) │────▶│ segments          │
 │ flights           │────▶│ 3. cluster.py (KMeans)│    │ segment_report    │
 │ airports          │     │ 4. report.py (write)  │    │ pipeline_log      │
 └───────────────────┘     └──────────────────────┘     └───────────────────┘
```

## RFM Feature Definitions

| Feature   | Definition                                      |
|-----------|-------------------------------------------------|
| Recency   | Days since most recent booking                  |
| Frequency | Number of completed (non-cancelled) bookings    |
| Monetary  | Total passenger spend (from booking totals)     |

### Reference Date

Recency is calculated relative to a fixed reference date: **2024-12-01**.
This ensures consistent results across pipeline runs.

## Clustering

- **Algorithm:** K-means
- **Number of clusters:** 5
- **Random state:** fixed (see config.py)
- **Preprocessing:** features are standardized to comparable scales before distance-based clustering

### Segment Names

| Cluster | Name                |
|---------|---------------------|
| 0       | Champions           |
| 1       | Loyal Customers     |
| 2       | Potential Loyalists |
| 3       | At Risk             |
| 4       | Hibernating         |

## Output Tables

### rfm_features
Per-passenger RFM scores used as clustering input.

### segments
Maps each passenger to their assigned segment (cluster ID + name).

### segment_report
Aggregated summary per segment: count, average recency, frequency, monetary.

### pipeline_log
Timestamped log entries for each pipeline step (extract → features →
clustering → reporting).

## Running the Pipeline

```bash
cd /app && python main.py
```

## Configuration

All settings are in `config.py`:
- Database connection parameters (via environment variables)
- Pipeline parameters: cluster count, random seed, reference date

## Data Volumes

Current dataset:
- ~5,000 passengers
- ~20,000 bookings
- ~35,000 flight legs

The pipeline processes all passengers in a single batch. For larger
deployments, consider partitioning by region or loyalty tier.
