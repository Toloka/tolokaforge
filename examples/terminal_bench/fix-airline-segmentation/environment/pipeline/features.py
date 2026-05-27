"""RFM feature engineering for passenger segmentation.

Computes Recency, Frequency, and Monetary features from booking
and flight data. These features drive the K-means clustering that
assigns passengers to segments.
"""
import pandas as pd
import numpy as np
from config import PIPELINE_CONFIG


def compute_recency(bookings_df):
    """Compute recency: days since last activity for each passenger."""
    reference_date = pd.to_datetime(PIPELINE_CONFIG['reference_date'])

    active_bookings = bookings_df[bookings_df['status'] != 'cancelled']
    last_activity = active_bookings.groupby('passenger_id')['departure_date'].max()
    last_activity = pd.to_datetime(last_activity)

    recency = (reference_date - last_activity).dt.days
    return recency.reset_index().rename(columns={'departure_date': 'recency'})


def compute_frequency(bookings_df):
    """Compute frequency: number of bookings per passenger.

    Provisional bookings (status='pending') are excluded since they have
    not been confirmed yet.
    """
    # Exclude unprocessed provisional bookings
    processed = bookings_df[bookings_df['status'] != 'pending']
    frequency = processed.groupby('passenger_id')['booking_id'].count()
    return frequency.reset_index().rename(columns={'booking_id': 'frequency'})


def compute_monetary(bookings_df, flights_df):
    """Compute monetary value: total spending per passenger.

    Aggregates fare data from the flights table, joined through active
    bookings to associate each flight leg with its passenger.
    """
    # Only include active bookings in monetary calculation
    active_bookings = bookings_df[bookings_df['status'] != 'cancelled']

    # Merge flights with bookings to get passenger linkage
    booking_flights = flights_df.merge(
        active_bookings[['booking_id', 'passenger_id']],
        on='booking_id'
    )

    # Aggregate at booking level first to handle multi-leg correctly
    booking_totals = booking_flights.groupby(
        ['passenger_id', 'booking_id']
    )['price'].sum().reset_index()

    # Sum all booking totals per passenger
    monetary = booking_totals.groupby('passenger_id')['price'].sum()
    return monetary.reset_index().rename(columns={'price': 'monetary'})


def compute_rfm_features(bookings_df, flights_df):
    """Compute all RFM features for passenger segmentation."""
    recency = compute_recency(bookings_df)
    frequency = compute_frequency(bookings_df)
    monetary = compute_monetary(bookings_df, flights_df)

    rfm = recency.merge(frequency, on='passenger_id')
    rfm = rfm.merge(monetary, on='passenger_id')

    print(f"  Computed RFM features for {len(rfm)} passengers")

    return rfm
