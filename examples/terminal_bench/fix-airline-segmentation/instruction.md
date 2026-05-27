# Fix Airline Passenger Segmentation Pipeline

An airline's data team runs a Python pipeline that segments passengers using RFM analysis (Recency, Frequency, Monetary). The pipeline reads booking and flight data from PostgreSQL, computes RFM features per passenger, runs K-means clustering (k=5), and writes segment assignments back to reporting tables.

The pipeline completes without errors — logs say "Segmentation completed successfully" and all output tables are populated — but the business team reports that the segments don't make sense. High-value frequent flyers are ending up in the same segment as infrequent travelers, and some validation metrics look off.

## Stack

- **Python 3.11** — pandas, numpy, scikit-learn, psycopg2
- **PostgreSQL 15** — all source and output data

## Code Layout

Pipeline code lives in `/app`:

- `/app/main.py` — pipeline entry point (orchestrates all steps)
- `/app/extract.py` — data extraction from PostgreSQL
- `/app/features.py` — RFM feature engineering
- `/app/cluster.py` — K-means clustering and segment assignment
- `/app/report.py` — writes results to reporting tables
- `/app/config.py` — database connection and pipeline configuration
- `/app/docs/pipeline_design.md` — pipeline design documentation

## Database Connection

- Host: `localhost` (env: `PG_HOST`)
- Port: `5432` (env: `PG_PORT`)
- Database: `airline_db` (env: `PG_DB`)
- User: `airline` (env: `PG_USER`)
- Password: `seg_pipeline_2024` (env: `PG_PASSWORD`)

## Source Tables

- `passengers` — passenger roster with loyalty tiers
- `bookings` — booking records with dates, prices, and status
- `flights` — individual flight legs linked to bookings
- `airports` — airport reference data

## Output Tables (pipeline writes here)

- `rfm_features` — computed RFM scores per passenger
- `segments` — segment assignments per passenger
- `segment_report` — aggregated segment summaries
- `pipeline_log` — pipeline execution log

## Your Task

1. Investigate the pipeline code and data to identify why the segmentation results are incorrect
2. Fix all bugs in the pipeline code
3. Re-run the pipeline to regenerate correct results: `cd /app && python main.py`
4. Verify that all output tables are populated with corrected values

## Constraints

- Do NOT change the database schema — downstream reports depend on the exact table structures
- Do NOT change the pipeline's CLI interface — it's called by an orchestration system
- The pipeline must still write to all the same output tables
- The K-means cluster count (k=5) must be preserved
