#!/usr/bin/env python3
"""Generate sample data for the airline segmentation database.

Populates PostgreSQL with passenger, booking, and flight records
from a representative data export.
"""
import random
import string
from datetime import date, timedelta

random.seed(42)  # deterministic output for consistent dev environments

# ---------- constants --------------------------------------------------

AIRPORTS = [
    ("JFK", "John F Kennedy Intl", "New York", "US"),
    ("LAX", "Los Angeles Intl", "Los Angeles", "US"),
    ("ORD", "O'Hare Intl", "Chicago", "US"),
    ("ATL", "Hartsfield-Jackson", "Atlanta", "US"),
    ("DFW", "Dallas/Fort Worth Intl", "Dallas", "US"),
    ("DEN", "Denver Intl", "Denver", "US"),
    ("SFO", "San Francisco Intl", "San Francisco", "US"),
    ("SEA", "Seattle-Tacoma Intl", "Seattle", "US"),
    ("MIA", "Miami Intl", "Miami", "US"),
    ("BOS", "Boston Logan Intl", "Boston", "US"),
    ("LHR", "Heathrow", "London", "GB"),
    ("CDG", "Charles de Gaulle", "Paris", "FR"),
    ("FRA", "Frankfurt Intl", "Frankfurt", "DE"),
    ("AMS", "Schiphol", "Amsterdam", "NL"),
    ("MAD", "Barajas", "Madrid", "ES"),
    ("FCO", "Fiumicino", "Rome", "IT"),
    ("NRT", "Narita Intl", "Tokyo", "JP"),
    ("SIN", "Changi", "Singapore", "SG"),
    ("DXB", "Dubai Intl", "Dubai", "AE"),
    ("SYD", "Kingsford Smith", "Sydney", "AU"),
]

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "David", "Elizabeth", "William", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Christopher", "Karen",
    "Charles", "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony",
    "Margaret", "Mark", "Sandra", "Donald", "Ashley", "Steven", "Dorothy",
    "Paul", "Kimberly", "Andrew", "Emily", "Joshua", "Donna", "Kenneth",
    "Michelle", "Kevin", "Carol", "Brian", "Amanda", "George", "Melissa",
    "Timothy", "Deborah", "Ronald", "Stephanie", "Edward", "Rebecca",
    "Jason", "Sharon", "Jeffrey", "Laura", "Ryan", "Cynthia",
    "Jacob", "Kathleen", "Gary", "Amy", "Nicholas", "Angela", "Eric",
    "Shirley", "Jonathan", "Anna", "Stephen", "Brenda", "Larry", "Pamela",
    "Justin", "Emma", "Scott", "Nicole", "Brandon", "Helen", "Benjamin",
    "Samantha", "Samuel", "Katherine", "Raymond", "Christine", "Gregory",
    "Debra", "Frank", "Rachel", "Alexander", "Carolyn", "Patrick", "Janet",
    "Jack", "Catherine", "Dennis", "Maria", "Jerry", "Heather",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green",
    "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz",
    "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris",
    "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan",
    "Cooper", "Peterson", "Bailey", "Reed", "Kelly", "Howard", "Ramos",
    "Kim", "Cox", "Ward", "Richardson", "Watson", "Brooks", "Chavez",
    "Wood", "James", "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes",
    "Price", "Alvarez", "Castillo", "Sanders", "Patel", "Myers", "Long",
    "Ross", "Foster", "Jimenez",
]

LOYALTY_TIERS = ["bronze", "silver", "gold", "platinum"]
TIER_WEIGHTS = [0.50, 0.25, 0.15, 0.10]

BOOKING_STATUSES = ["confirmed", "cancelled", "pending"]

NUM_PASSENGERS = 5000
BOOKING_DATE_START = date(2023, 1, 1)
BOOKING_DATE_END = date(2024, 11, 30)
REFERENCE_DATE = date(2024, 12, 1)

# Departure can be up to 90 days after booking date
DEPARTURE_MAX_OFFSET = 90


# ---------- helpers ----------------------------------------------------

def random_date(start, end):
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def make_booking_ref():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def escape_sql(s):
    return s.replace("'", "''")


# ---------- generate ---------------------------------------------------

lines = []

# airports
lines.append("-- Airport data")
for code, name, city, country in AIRPORTS:
    lines.append(
        f"INSERT INTO airports (code, name, city, country) VALUES "
        f"('{code}', '{escape_sql(name)}', '{escape_sql(city)}', '{country}');"
    )

# passengers
lines.append("\n-- Passenger data")
EMAIL_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "protonmail.com", "aol.com", "mail.com", "zoho.com", "yandex.com",
]
emails_seen = set()
for pid in range(1, NUM_PASSENGERS + 1):
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    tier = random.choices(LOYALTY_TIERS, weights=TIER_WEIGHTS, k=1)[0]
    domain = random.choice(EMAIL_DOMAINS)
    email = f"{fn.lower()}.{ln.lower()}.{pid}@{domain}"
    while email in emails_seen:
        domain = random.choice(EMAIL_DOMAINS)
        email = f"{fn.lower()}.{ln.lower()}.{pid}.{random.randint(1,999)}@{domain}"
    emails_seen.add(email)
    lines.append(
        f"INSERT INTO passengers (id, first_name, last_name, email, loyalty_tier) VALUES "
        f"({pid}, '{escape_sql(fn)}', '{escape_sql(ln)}', '{escape_sql(email)}', '{tier}');"
    )

# bookings and flights
lines.append("\n-- Booking and flight data")
booking_id = 0
flight_id = 0
refs_seen = set()
airport_codes = [a[0] for a in AIRPORTS]

for pid in range(1, NUM_PASSENGERS + 1):
    # Each passenger gets 2-8 bookings (avg ~4)
    n_bookings = random.choices(range(2, 9), weights=[10, 20, 25, 20, 12, 7, 4], k=1)[0]

    for booking_idx in range(n_bookings):
        booking_id += 1

        booking_date = random_date(BOOKING_DATE_START, BOOKING_DATE_END)

        # Departure 1-90 days after booking
        dep_offset = random.randint(1, DEPARTURE_MAX_OFFSET)
        departure_date = booking_date + timedelta(days=dep_offset)

        # Pick origin and destination
        origin = random.choice(airport_codes)
        dest = random.choice([c for c in airport_codes if c != origin])

        # ~20% cancelled, ~5% pending (first booking always confirmed
        # to ensure every passenger has at least one active booking)
        if booking_idx == 0:
            status = 'confirmed'
        else:
            status = random.choices(BOOKING_STATUSES, weights=[75, 20, 5], k=1)[0]

        # Fare before discounts (what the legs add up to)
        base_fare = round(random.uniform(150, 3200), 2)

        # 40% of bookings get a promo/corporate/loyalty discount (5-15%)
        if random.random() < 0.40:
            discount_pct = random.uniform(0.05, 0.15)
            total_price = round(base_fare * (1 - discount_pct), 2)
        else:
            total_price = base_fare

        ref = make_booking_ref()
        while ref in refs_seen:
            ref = make_booking_ref()
        refs_seen.add(ref)

        lines.append(
            f"INSERT INTO bookings (id, passenger_id, booking_date, departure_date, "
            f"origin, destination, total_price, status, booking_ref) VALUES "
            f"({booking_id}, {pid}, '{booking_date}', '{departure_date}', "
            f"'{origin}', '{dest}', {total_price}, '{status}', '{ref}');"
        )

        # Flights: 60% direct (1 leg), 30% connecting (2 legs), 10% multi-stop (3 legs)
        n_legs = random.choices([1, 2, 3], weights=[60, 30, 10], k=1)[0]

        if n_legs == 1:
            flight_id += 1
            fn_num = f"AL{random.randint(100,999)}"
            arr_date = departure_date + timedelta(hours=random.randint(2, 14))
            lines.append(
                f"INSERT INTO flights (id, booking_id, flight_number, departure_airport, "
                f"arrival_airport, departure_date, arrival_date, price, leg_order) VALUES "
                f"({flight_id}, {booking_id}, '{fn_num}', '{origin}', '{dest}', "
                f"'{departure_date}', '{arr_date}', {base_fare}, 1);"
            )
        else:
            # Split fare among legs (not equally — varies)
            leg_airports = [origin]
            available = [c for c in airport_codes if c != origin and c != dest]
            stops = random.sample(available, min(n_legs - 1, len(available)))
            leg_airports.extend(stops)
            leg_airports.append(dest)

            remaining = base_fare
            leg_prices = []
            for i in range(n_legs):
                if i == n_legs - 1:
                    leg_prices.append(round(remaining, 2))
                else:
                    p = round(random.uniform(0.2, 0.6) * base_fare, 2)
                    p = min(p, remaining - 0.01 * (n_legs - i - 1))
                    leg_prices.append(round(p, 2))
                    remaining -= p

            current_date = departure_date
            for leg in range(n_legs):
                flight_id += 1
                fn_num = f"AL{random.randint(100,999)}"
                arr_date = current_date + timedelta(hours=random.randint(2, 8))

                dep_apt = leg_airports[leg]
                arr_apt = leg_airports[leg + 1]

                lines.append(
                    f"INSERT INTO flights (id, booking_id, flight_number, departure_airport, "
                    f"arrival_airport, departure_date, arrival_date, price, leg_order) VALUES "
                    f"({flight_id}, {booking_id}, '{fn_num}', '{dep_apt}', '{arr_apt}', "
                    f"'{current_date}', '{arr_date}', {leg_prices[leg]}, {leg + 1});"
                )
                # Next leg departs next day (layover)
                current_date = current_date + timedelta(days=1)

# Reset sequences
lines.append(f"\nSELECT setval('passengers_id_seq', {NUM_PASSENGERS});")
lines.append(f"SELECT setval('bookings_id_seq', {booking_id});")
lines.append(f"SELECT setval('flights_id_seq', {flight_id});")

# Write output
with open("/tmp/seed_data.sql", "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"Generated seed data: {NUM_PASSENGERS} passengers, {booking_id} bookings, {flight_id} flights")
