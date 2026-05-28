-- Airline Segmentation Database Schema
-- =====================================
-- This schema supports the passenger segmentation pipeline.
-- Tables are divided into source data (bookings, flights, passengers, airports)
-- and pipeline output (rfm_features, segments, segment_report, pipeline_log).

-- Source tables ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS airports (
    code        VARCHAR(3) PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    city        VARCHAR(100) NOT NULL,
    country     VARCHAR(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS passengers (
    id          SERIAL PRIMARY KEY,
    first_name  VARCHAR(50) NOT NULL,
    last_name   VARCHAR(50) NOT NULL,
    email       VARCHAR(120) UNIQUE NOT NULL,
    loyalty_tier VARCHAR(20) DEFAULT 'bronze',
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bookings (
    id              SERIAL PRIMARY KEY,
    passenger_id    INTEGER NOT NULL REFERENCES passengers(id),
    booking_date    DATE NOT NULL,
    departure_date  DATE NOT NULL,
    origin          VARCHAR(3) NOT NULL REFERENCES airports(code),
    destination     VARCHAR(3) NOT NULL REFERENCES airports(code),
    total_price     NUMERIC(10,2) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'confirmed',
    booking_ref     VARCHAR(10) UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS flights (
    id                  SERIAL PRIMARY KEY,
    booking_id          INTEGER NOT NULL REFERENCES bookings(id),
    flight_number       VARCHAR(10) NOT NULL,
    departure_airport   VARCHAR(3) NOT NULL REFERENCES airports(code),
    arrival_airport     VARCHAR(3) NOT NULL REFERENCES airports(code),
    departure_date      DATE NOT NULL,
    arrival_date        DATE NOT NULL,
    price               NUMERIC(10,2) NOT NULL,
    leg_order           INTEGER NOT NULL DEFAULT 1
);

-- Pipeline output tables ------------------------------------------------

CREATE TABLE IF NOT EXISTS rfm_features (
    passenger_id    INTEGER PRIMARY KEY REFERENCES passengers(id),
    recency         NUMERIC(10,2) NOT NULL,
    frequency       INTEGER NOT NULL,
    monetary        NUMERIC(12,2) NOT NULL,
    computed_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS segments (
    passenger_id    INTEGER PRIMARY KEY REFERENCES passengers(id),
    segment_id      INTEGER NOT NULL,
    segment_name    VARCHAR(50) NOT NULL,
    assigned_at     TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS segment_report (
    segment_id      INTEGER PRIMARY KEY,
    segment_name    VARCHAR(50) NOT NULL,
    passenger_count INTEGER NOT NULL,
    avg_recency     NUMERIC(10,2),
    avg_frequency   NUMERIC(10,2),
    avg_monetary    NUMERIC(12,2),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_log (
    id          SERIAL PRIMARY KEY,
    step        VARCHAR(50) NOT NULL,
    status      VARCHAR(20) NOT NULL,
    message     TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_bookings_passenger ON bookings(passenger_id);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
CREATE INDEX IF NOT EXISTS idx_flights_booking ON flights(booking_id);
