import os

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "billing_db")
PG_USER = os.getenv("PG_USER", "billing")
PG_PASSWORD = os.getenv("PG_PASSWORD", "billing")

DATABASE_URL = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"
