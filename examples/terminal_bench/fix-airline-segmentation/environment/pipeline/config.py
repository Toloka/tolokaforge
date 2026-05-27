"""Configuration for the airline segmentation pipeline."""
import os

DB_CONFIG = {
    'host': os.getenv('PG_HOST', 'localhost'),
    'port': int(os.getenv('PG_PORT', '5432')),
    'dbname': os.getenv('PG_DB', 'airline_db'),
    'user': os.getenv('PG_USER', 'airline'),
    'password': os.getenv('PG_PASSWORD', 'seg_pipeline_2024'),
}

PIPELINE_CONFIG = {
    'n_clusters': 5,
    'random_state': 42,
    'reference_date': '2024-12-01',
}
