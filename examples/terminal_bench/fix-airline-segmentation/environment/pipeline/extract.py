"""Data extraction module — reads raw data from PostgreSQL."""
import psycopg2
import pandas as pd
from config import DB_CONFIG


def get_connection():
    """Create a new database connection."""
    return psycopg2.connect(**DB_CONFIG)


def extract_bookings():
    """Extract all bookings with passenger and date information."""
    conn = get_connection()
    query = """
        SELECT b.id AS booking_id,
               b.passenger_id,
               b.booking_date,
               b.departure_date,
               b.total_price,
               b.status
          FROM bookings b
    """
    df = pd.read_sql(query, conn)
    conn.close()
    return df


def extract_flights():
    """Extract all flight legs with pricing."""
    conn = get_connection()
    query = """
        SELECT f.id AS flight_id,
               f.booking_id,
               f.flight_number,
               f.departure_airport,
               f.arrival_airport,
               f.price,
               f.leg_order
          FROM flights f
    """
    df = pd.read_sql(query, conn)
    conn.close()
    return df


def extract_passengers():
    """Extract passenger roster."""
    conn = get_connection()
    query = """
        SELECT id AS passenger_id,
               first_name,
               last_name,
               loyalty_tier
          FROM passengers
    """
    df = pd.read_sql(query, conn)
    conn.close()
    return df
