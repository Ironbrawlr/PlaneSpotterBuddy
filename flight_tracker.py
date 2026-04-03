#!/usr/bin/env python3
"""
Real-time flight tracking notification system for aircraft approaching Singapore (SIN).

Features:
- Fetches live aircraft data from ADS-B Exchange via RapidAPI
- Filters aircraft near Singapore
- Detects likely arriving flights using altitude, distance, and trend heuristics
- Estimates ETA to Singapore Changi Airport
- Flags rare aircraft types (A380 / B747 variants)
- Scrapes Flightradar24 for possible special liveries
- Applies FR24 global rate limiting and livery result caching
- Sends Telegram alerts once per callsign
"""

import math
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

# =========================
# Configuration
# =========================

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "YOUR_RAPIDAPI_KEY")
RAPIDAPI_HOST = "adsbexchange-com1.p.rapidapi.com"
ADSB_API_URL = "https://adsbexchange-com1.p.rapidapi.com/v2/lat/1.5/lon/104.0/dist/250/"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

REQUEST_TIMEOUT = 20
MAIN_LOOP_INTERVAL_SECONDS = 60
FR24_MIN_INTERVAL_SECONDS = 2.0

# Singapore bounding box
MIN_LAT = 0.5
MAX_LAT = 2.5
MIN_LON = 103.0
MAX_LON = 105.0

# Singapore Changi Airport
CHANGI_LAT = 1.3644
CHANGI_LON = 103.9915

# Flight heuristics
ARRIVAL_ALTITUDE_THRESHOLD_FT = 20000
WITHIN_DISTANCE_KM = 120.0
LIVERY_ALTITUDE_THRESHOLD_FT = 15000
ETA_ALERT_THRESHOLD_MINUTES = 30.0

# Rare aircraft types
RARE_AIRCRAFT_TYPES = {"A388", "B744", "B748"}

# Livery keywords
KEYWORDS = ["livery", "retro", "expo", "special", "star alliance"]

# Runtime state
livery_cache: Dict[str, bool] = {}
seen_flights: Set[str] = set()
previous_state: Dict[str, Dict[str, float]] = {}
last_fr24_request_time: float = 0.0

# Shared HTTP session
session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; SingaporeFlightTracker/1.0)",
    }
)


def fetch_adsb_data() -> List[Dict[str, Any]]:
    """Fetch aircraft data from ADS-B Exchange via RapidAPI."""
    print("Fetching flights from ADS-B Exchange...")

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": RAPIDAPI_HOST,
        "Accept": "application/json",
    }

    try:
        response = session.get(
            ADSB_API_URL,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        aircraft = data.get("ac", [])
        if not isinstance(aircraft, list):
            print("ADS-B response did not include a valid 'ac' list.")
            return []

        print(f"Fetched {len(aircraft)} flights.")
        return aircraft

    except requests.exceptions.Timeout:
        print("ADS-B request timed out.")
        return []
    except requests.exceptions.RequestException as exc:
        print(f"ADS-B request failed: {exc}")
        return []
    except ValueError as exc:
        print(f"Failed to parse ADS-B JSON response: {exc}")
        return []


def is_inside_singapore_box(lat: Any, lon: Any) -> bool:
    """Check whether coordinates are inside the Singapore bounding box."""
    try:
        lat_value = float(lat)
        lon_value = float(lon)
    except (TypeError, ValueError):
        return False

    return MIN_LAT <= lat_value <= MAX_LAT and MIN_LON <= lon_value <= MAX_LON


def normalize_callsign(flight: Dict[str, Any]) -> Optional[str]:
    """Normalize the flight callsign."""
    callsign = flight.get("flight")
    if not callsign:
        return None

    callsign = str(callsign).strip().upper()
    return callsign if callsign else None


def normalize_registration(flight: Dict[str, Any]) -> Optional[str]:
    """Normalize the aircraft registration."""
    registration = flight.get("r")
    if not registration:
        return None

    registration = str(registration).strip().upper()
    return registration if registration else None


def normalize_aircraft_type(flight: Dict[str, Any]) -> Optional[str]:
    """Normalize the aircraft ICAO type."""
    aircraft_type = flight.get("t")
    if not aircraft_type:
        return None

    aircraft_type = str(aircraft_type).strip().upper()
    return aircraft_type if aircraft_type else None


def normalize_altitude(flight: Dict[str, Any]) -> Optional[int]:
    """Normalize altitude in feet."""
    altitude = flight.get("alt_baro")
    if altitude is None:
        return None

    if isinstance(altitude, str) and altitude.lower() in {"ground", "gnd"}:
        return 0

    try:
        return int(float(altitude))
    except (TypeError, ValueError):
        return None


def normalize_ground_speed_knots(flight: Dict[str, Any]) -> Optional[float]:
    """Normalize ground speed in knots."""
    speed = flight.get("gs")
    if speed is None:
        return None

    try:
        speed_value = float(speed)
        return speed_value if speed_value >= 0 else None
    except (TypeError, ValueError):
        return None


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in kilometers."""
    earth_radius_km = 6371.0

    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    delta_lat = lat2_rad - lat1_rad
    delta_lon = lon2_rad - lon1_rad

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return earth_radius_km * c


def estimate_eta(distance_km: float, ground_speed_knots: Optional[float]) -> Optional[float]:
    """Estimate ETA in minutes using distance and ground speed."""
    if ground_speed_knots is None or ground_speed_knots <= 0:
        return None

    ground_speed_kmh = ground_speed_knots * 1.852
    if ground_speed_kmh <= 0:
        return None

    eta_minutes = (distance_km / ground_speed_kmh) * 60.0
    if eta_minutes <= 0:
        return None

    return eta_minutes


def is_arriving_flight(
    callsign: str,
    altitude_ft: Optional[int],
    distance_km: float,
) -> bool:
    """
    Determine whether a flight is likely arriving at Singapore using heuristics:
    - altitude < 20,000 ft
    - altitude decreasing (if previous state exists)
    - OR distance decreasing
    - OR within 120 km of airport
    """
    altitude_low = altitude_ft is not None and altitude_ft < ARRIVAL_ALTITUDE_THRESHOLD_FT
    within_range = distance_km <= WITHIN_DISTANCE_KM

    previous = previous_state.get(callsign)
    altitude_decreasing = False
    distance_decreasing = False

    if previous:
        previous_altitude = previous.get("altitude_ft")
        previous_distance = previous.get("distance_km")

        if altitude_ft is not None and previous_altitude is not None:
            altitude_decreasing = altitude_ft < previous_altitude

        if previous_distance is not None:
            distance_decreasing = distance_km < previous_distance

    return altitude_low and (altitude_decreasing or distance_decreasing or within_range)


def apply_fr24_rate_limit() -> None:
    """Enforce a global minimum interval between FR24 requests."""
    global last_fr24_request_time

    now = time.time()
    elapsed = now - last_fr24_request_time
    if elapsed < FR24_MIN_INTERVAL_SECONDS:
        sleep_for = FR24_MIN_INTERVAL_SECONDS - elapsed
        time.sleep(sleep_for)

    last_fr24_request_time = time.time()


def has_special_livery(registration: Optional[str]) -> bool:
    """
    Check whether the aircraft likely has a special livery by scraping FR24.
    Returns False on failure.
    """
    if not registration:
        return False

    if registration in livery_cache:
        print(f"Using cached livery result for {registration}: {livery_cache[registration]}")
        return livery_cache[registration]

    print(f"Checking livery for {registration}...")

    apply_fr24_rate_limit()
    url = f"https://www.flightradar24.com/data/aircraft/{registration}"

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True).lower()
        result = any(keyword in page_text for keyword in KEYWORDS)

        livery_cache[registration] = result
        print(f"Livery check result for {registration}: {result}")
        return result

    except requests.exceptions.Timeout:
        print(f"FR24 request timed out for {registration}.")
    except requests.exceptions.RequestException as exc:
        print(f"FR24 request failed for {registration}: {exc}")
    except Exception as exc:
        print(f"FR24 parsing failed for {registration}: {exc}")

    livery_cache[registration] = False
    return False


def send_telegram(message: str) -> None:
    """Send a Telegram alert."""
    print("Sending Telegram alert...")

    if (
        TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN"
        or TELEGRAM_CHAT_ID == "YOUR_TELEGRAM_CHAT_ID"
    ):
        print("Telegram credentials not configured. Alert message:")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        response = session.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        print("Alert sent successfully.")
    except requests.exceptions.Timeout:
        print("Telegram request timed out.")
    except requests.exceptions.RequestException as exc:
        print(f"Telegram request failed: {exc}")


def trim_seen_flights() -> None:
    """Prevent the seen_flights set from growing indefinitely."""
    global seen_flights

    if len(seen_flights) > 500:
        print("seen_flights exceeded 500 entries. Clearing set.")
        seen_flights.clear()


def process_flights(flights: List[Dict[str, Any]]) -> None:
    """Filter, score, and alert on qualifying flights."""
    passing_filter_count = 0

    for flight in flights:
        lat = flight.get("lat")
        lon = flight.get("lon")

        if not is_inside_singapore_box(lat, lon):
            continue

        callsign = normalize_callsign(flight)
        if not callsign:
            continue

        try:
            lat_value = float(lat)
            lon_value = float(lon)
        except (TypeError, ValueError):
            continue

        altitude_ft = normalize_altitude(flight)
        registration = normalize_registration(flight) or "UNKNOWN"
        aircraft_type = normalize_aircraft_type(flight) or "UNKNOWN"
        ground_speed_knots = normalize_ground_speed_knots(flight)

        distance_km = haversine_distance(lat_value, lon_value, CHANGI_LAT, CHANGI_LON)
        eta_minutes = estimate_eta(distance_km, ground_speed_knots)

        arriving = is_arriving_flight(callsign, altitude_ft, distance_km)

        previous_state[callsign] = {
            "altitude_ft": float(altitude_ft) if altitude_ft is not None else None,
            "distance_km": distance_km,
        }

        if not arriving:
            continue

        passing_filter_count += 1

        if eta_minutes is None:
            print(f"{callsign}: ETA unavailable.")
            continue

        print(
            f"{callsign}: distance={distance_km:.1f} km, "
            f"gs={ground_speed_knots if ground_speed_knots is not None else 'N/A'} kt, "
            f"eta={eta_minutes:.1f} min"
        )

        if not (0 < eta_minutes <= ETA_ALERT_THRESHOLD_MINUTES):
            continue

        if callsign in seen_flights:
            continue

        reason: Optional[str] = None

        if aircraft_type in RARE_AIRCRAFT_TYPES:
            reason = "Widebody"
        elif altitude_ft is not None and altitude_ft < LIVERY_ALTITUDE_THRESHOLD_FT:
            actual_registration = normalize_registration(flight)
            if has_special_livery(actual_registration):
                reason = "Special Livery"

        if reason:
            eta_display = max(1, int(round(eta_minutes)))
            message = (
                "✈️ Rare Aircraft Detected!\n"
                f"Callsign: {callsign}\n"
                f"Type: {aircraft_type}\n"
                f"Registration: {registration}\n"
                f"ETA: {eta_display} minutes\n"
                f"Reason: {reason}"
            )
            send_telegram(message)
            seen_flights.add(callsign)

    print(f"Flights passing arrival filters: {passing_filter_count}")
    trim_seen_flights()


def main() -> None:
    """Main monitoring loop."""
    print("Starting Singapore real-time flight tracking notifier...")

    while True:
        try:
            flights = fetch_adsb_data()
            process_flights(flights)
        except Exception as exc:
            print(f"Unexpected error in main loop: {exc}")

        print(f"Sleeping for {MAIN_LOOP_INTERVAL_SECONDS} seconds...\n")
        time.sleep(MAIN_LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

