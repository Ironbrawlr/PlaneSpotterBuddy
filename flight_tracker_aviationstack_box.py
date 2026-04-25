#!/usr/bin/env python3
"""
Singapore rare flight tracker with Telegram preference selectors and
manual scan control.

This version is Aviationstack-only and uses a configurable latitude /
longitude alert zone instead of a distance radius.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

# =========================
# Configuration
# =========================

AVIATIONSTACK_ACCESS_KEY = os.getenv("AVIATIONSTACK_ACCESS_KEY", "YOUR_AVIATIONSTACK_ACCESS_KEY")
AVIATIONSTACK_URL = "http://api.aviationstack.com/v1/flights"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Keep the new box-based bot isolated from the earlier script's saved state.
PREFERENCES_FILE = os.getenv("TELEGRAM_PREFERENCES_FILE", "user_preferences_box.json")
STATE_FILE = os.getenv("BOT_STATE_FILE", "bot_state_box.json")

REQUEST_TIMEOUT = 20
BOT_POLL_INTERVAL_SECONDS = 5
SCAN_INTERVAL_SECONDS = 600  # 10 minutes
FR24_MIN_INTERVAL_SECONDS = 2.0

# Singapore alert zone for inbound traffic.
# Tune these if you want a wider / tighter approach area.
ALERT_MIN_LAT = float(os.getenv("ALERT_MIN_LAT", "0.8"))
ALERT_MAX_LAT = float(os.getenv("ALERT_MAX_LAT", "2.2"))
ALERT_MIN_LON = float(os.getenv("ALERT_MIN_LON", "103.3"))
ALERT_MAX_LON = float(os.getenv("ALERT_MAX_LON", "104.7"))

RARE_AIRCRAFT_TYPES = {"A388", "B744", "B748"}
LIVERY_ALTITUDE_THRESHOLD_FT = 15000
# Keep livery matching narrow so generic FR24 page text does not create
# false positives.
LIVERY_KEYWORDS = ["retro livery", "special livery", "expo", "star alliance"]

AIRLINE_OPTIONS = {
    "SIA": "Singapore Airlines",
    "SQC": "Singapore Cargo",
    "BAW": "British Airways",
    "QFA": "Qantas",
    "UAE": "Emirates",
    "DLH": "Lufthansa",
    "AFR": "Air France",
    "KLM": "KLM",
    "CPA": "Cathay Pacific",
    "THA": "Thai Airways",
    "TGW": "Scoot",
    "IGO": "IndiGo",
    "AXM": "AirAsia",
    "XAX": "AirAsia X",
    "AIQ": "Thai AirAsia",
    "TAX": "Thai AirAsia X",
    "AWQ": "Indonesia AirAsia",
    "APG": "Philippines AirAsia",
    "ANA": "ANA",
    "JAL": "Japan Airlines",
    "CCA": "Air China",
    "CES": "China Eastern",
    "CSN": "China Southern",
    "SWR": "Swiss",
    "ETD": "Etihad",
    "AIC": "Air India",
    "FDX": "FedEx Express",
    "KME": "Cambodia Airways",
}

AIRLINE_IATA_TO_ICAO = {
    "SQ": "SIA",
    "TR": "TGW",
    "6E": "IGO",
    "LH": "DLH",
    "TG": "THA",
    "FX": "FDX",
    "KR": "KME",
}

AIRCRAFT_LABELS = {
    "A388": "A380",
    "B744": "747-400",
    "B748": "747-8",
}

# =========================
# Runtime state
# =========================

session = requests.Session()
session.headers.update(
    {"User-Agent": "Mozilla/5.0 (compatible; SingaporeRareFlightTrackerBox/1.0)"}
)

livery_cache: Dict[str, List[str]] = {}
seen_alerts: Dict[str, Set[str]] = {}
user_preferences: Dict[str, Dict[str, Any]] = {}
bot_state: Dict[str, Any] = {}
telegram_offset: int = 0
last_fr24_request_time: float = 0.0
next_scan_time: float = 0.0


# =========================
# Persistence
# =========================

def default_preferences() -> Dict[str, Any]:
    return {
        "rare_types": sorted(list(RARE_AIRCRAFT_TYPES)),
        "aircraft_mode": "selected",  # selected | any
        "livery_mode": "any",  # off | any | specific
        "livery_keywords": [],
        "airlines": [],
        "alert_selected_airlines": False,
    }


def default_bot_state() -> Dict[str, Any]:
    return {
        "scanning_enabled": False,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "last_scan_time": 0.0,
        "scan_count": 0,
    }


def load_json_file(path: str, fallback: Any) -> Any:
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        print(f"Failed to load {path}: {exc}")
        return fallback


def save_json_file(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
    except Exception as exc:
        print(f"Failed to save {path}: {exc}")


def load_preferences() -> Dict[str, Dict[str, Any]]:
    data = load_json_file(PREFERENCES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_preferences() -> None:
    save_json_file(PREFERENCES_FILE, user_preferences)


def load_bot_state() -> Dict[str, Any]:
    data = load_json_file(STATE_FILE, default_bot_state())
    if not isinstance(data, dict):
        return default_bot_state()
    merged = default_bot_state()
    merged.update(data)
    return merged


def save_bot_state() -> None:
    save_json_file(STATE_FILE, bot_state)


def get_user_preferences(chat_id: int) -> Dict[str, Any]:
    chat_key = str(chat_id)
    if chat_key not in user_preferences:
        user_preferences[chat_key] = default_preferences()
        save_preferences()
    return user_preferences[chat_key]


def get_seen_alerts(chat_id: int) -> Set[str]:
    chat_key = str(chat_id)
    if chat_key not in seen_alerts:
        seen_alerts[chat_key] = set()
    return seen_alerts[chat_key]


# =========================
# Telegram API
# =========================

def telegram_api(method: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        print(f"Telegram not configured. Skipping {method}.")
        return None

    url = f"{TELEGRAM_API_BASE}/{method}"

    try:
        response = session.post(url, json=payload or {}, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            print(f"Telegram API error in {method}: {data}")
            return None
        return data
    except requests.exceptions.Timeout:
        print(f"Telegram {method} timed out.")
        return None
    except requests.exceptions.RequestException as exc:
        print(f"Telegram {method} failed: {exc}")
        return None
    except ValueError as exc:
        print(f"Telegram {method} returned invalid JSON: {exc}")
        return None


def send_telegram(chat_id: int, message: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": message,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_api("sendMessage", payload)


def edit_telegram_message(chat_id: int, message_id: int, text: str, reply_markup: Dict[str, Any]) -> None:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": reply_markup,
    }
    telegram_api("editMessageText", payload)


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    telegram_api("answerCallbackQuery", payload)


def fetch_telegram_updates() -> List[Dict[str, Any]]:
    global telegram_offset

    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        return []

    payload = {
        "timeout": 0,
        "offset": telegram_offset,
        "allowed_updates": ["message", "callback_query"],
    }
    result = telegram_api("getUpdates", payload)
    if not result:
        return []

    updates = result.get("result", [])
    if updates:
        telegram_offset = updates[-1]["update_id"] + 1
    return updates


# =========================
# Telegram UI
# =========================

def checkbox(selected: bool) -> str:
    return "✅" if selected else "☑️"


def build_settings_summary(chat_id: int) -> str:
    prefs = get_user_preferences(chat_id)

    rare_types = prefs.get("rare_types", [])
    if prefs.get("aircraft_mode", "selected") == "any":
        aircraft_text = "Any aircraft type"
    else:
        aircraft_text = ", ".join(
            f"{code} ({AIRCRAFT_LABELS.get(code, code)})" for code in rare_types
        ) if rare_types else "None"

    livery_mode = prefs.get("livery_mode", "any")
    if livery_mode == "off":
        livery_text = "Off"
    elif livery_mode == "any":
        livery_text = "Any livery"
    else:
        keywords = prefs.get("livery_keywords", [])
        livery_text = f"Specific: {', '.join(keywords) if keywords else 'None selected'}"

    airlines = prefs.get("airlines", [])
    airline_text = ", ".join(
        f"{code} ({AIRLINE_OPTIONS.get(code, code)})" for code in airlines
    ) if airlines else "Any airline"
    airline_only_text = "On" if prefs.get("alert_selected_airlines", False) else "Off"

    return (
        "Flight Alert Preferences\n\n"
        f"Aircraft: {aircraft_text}\n"
        f"Livery: {livery_text}\n"
        f"Airlines: {airline_text}\n\n"
        f"Airline-only alerts: {airline_only_text}\n\n"
        "Choose a section below to update your alerts."
    )


def build_main_menu() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Aircraft Types", "callback_data": "menu:aircraft"}],
            [{"text": "Livery Alerts", "callback_data": "menu:livery"}],
            [{"text": "Airlines", "callback_data": "menu:airline"}],
            [{"text": "Reset To Default", "callback_data": "action:reset"}],
        ]
    }


def build_aircraft_menu(chat_id: int) -> Dict[str, Any]:
    prefs = get_user_preferences(chat_id)
    selected = set(prefs.get("rare_types", []))
    aircraft_mode = prefs.get("aircraft_mode", "selected")
    rows = []

    rows.append(
        [{
            "text": f"{checkbox(aircraft_mode == 'any')} Any",
            "callback_data": "set:aircraft_mode:any",
        }]
    )

    for aircraft_type in sorted(RARE_AIRCRAFT_TYPES):
        label = (
            f"{checkbox(aircraft_type in selected)} "
            f"{aircraft_type} ({AIRCRAFT_LABELS.get(aircraft_type, aircraft_type)})"
        )
        rows.append([{"text": label, "callback_data": f"toggle:aircraft:{aircraft_type}"}])

    rows.append([{"text": "Back", "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def build_livery_menu(chat_id: int) -> Dict[str, Any]:
    prefs = get_user_preferences(chat_id)
    mode = prefs.get("livery_mode", "any")

    return {
        "inline_keyboard": [
            [{"text": f"{checkbox(mode == 'off')} Off", "callback_data": "set:livery:off"}],
            [{"text": f"{checkbox(mode == 'any')} Any Livery", "callback_data": "set:livery:any"}],
            [{"text": f"{checkbox(mode == 'specific')} Specific Livery", "callback_data": "set:livery:specific"}],
            [{"text": "Choose Specific Keywords", "callback_data": "menu:keywords"}],
            [{"text": "Back", "callback_data": "menu:main"}],
        ]
    }


def build_keyword_menu(chat_id: int) -> Dict[str, Any]:
    prefs = get_user_preferences(chat_id)
    selected = set(prefs.get("livery_keywords", []))
    rows = []

    for keyword in LIVERY_KEYWORDS:
        label = f"{checkbox(keyword in selected)} {keyword}"
        rows.append([{"text": label, "callback_data": f"toggle:keyword:{keyword}"}])

    rows.append([{"text": "Back", "callback_data": "menu:livery"}])
    return {"inline_keyboard": rows}


def build_airline_menu(chat_id: int) -> Dict[str, Any]:
    prefs = get_user_preferences(chat_id)
    selected = set(prefs.get("airlines", []))
    airline_only = prefs.get("alert_selected_airlines", False)
    rows = [[{"text": f"{checkbox(not selected)} Any Airline", "callback_data": "set:airline:any"}]]

    for code, name in AIRLINE_OPTIONS.items():
        label = f"{checkbox(code in selected)} {code} ({name})"
        rows.append([{"text": label, "callback_data": f"toggle:airline:{code}"}])

    rows.append(
        [{
            "text": f"{checkbox(airline_only)} Alert On Selected Airlines",
            "callback_data": "toggle:airline_mode:selected",
        }]
    )
    rows.append([{"text": "Back", "callback_data": "menu:main"}])
    return {"inline_keyboard": rows}


def get_menu_text(menu_name: str, chat_id: int) -> Tuple[str, Dict[str, Any]]:
    if menu_name == "aircraft":
        return "Choose the rare aircraft types you want alerts for.", build_aircraft_menu(chat_id)
    if menu_name == "livery":
        return "Choose whether you want livery alerts, and how specific they should be.", build_livery_menu(chat_id)
    if menu_name == "keywords":
        return "Choose the livery keywords you want when 'Specific Livery' mode is enabled.", build_keyword_menu(chat_id)
    if menu_name == "airline":
        return "Choose which airlines you want alerts for. Select none for any airline.", build_airline_menu(chat_id)
    return build_settings_summary(chat_id), build_main_menu()


def build_start_message() -> str:
    return (
        "Singapore rare flight tracker is ready.\n\n"
        "Commands:\n"
        "/start - show this help message\n"
        "/settings - open alert preferences\n"
        "/status - show bot and scan status\n"
        "/scanonce - run one scan now\n"
        "/scanon - turn scheduled scanning on\n"
        "/scanoff - turn scheduled scanning off\n\n"
        "Current alert zone:\n"
        f"Latitude {ALERT_MIN_LAT} to {ALERT_MAX_LAT}\n"
        f"Longitude {ALERT_MIN_LON} to {ALERT_MAX_LON}"
    )


# =========================
# Flight data helpers
# =========================

def extract_aviationstack_flights(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []

    for item in data.get("data", []):
        flight_info = item.get("flight", {}) or {}
        aircraft_info = item.get("aircraft", {}) or {}
        live_info = item.get("live", {}) or {}
        airline_info = item.get("airline", {}) or {}

        lat = live_info.get("latitude")
        lon = live_info.get("longitude")
        altitude_m = live_info.get("altitude")
        speed_kmh = live_info.get("speed_horizontal")

        alt_baro = None
        if altitude_m is not None:
            try:
                alt_baro = float(altitude_m) * 3.28084
            except (TypeError, ValueError):
                alt_baro = None

        gs = None
        if speed_kmh is not None:
            try:
                gs = float(speed_kmh) / 1.852
            except (TypeError, ValueError):
                gs = None

        airline_icao = airline_info.get("icao") or ""
        airline_iata = airline_info.get("iata") or ""
        flight_number = flight_info.get("number")

        callsign = None
        if airline_icao and flight_number:
            callsign = f"{airline_icao}{flight_number}"
        elif airline_iata and flight_number:
            callsign = f"{airline_iata}{flight_number}"
        else:
            callsign = flight_info.get("icao") or flight_info.get("iata") or flight_number

        output.append(
            {
                "flight": callsign,
                "r": aircraft_info.get("registration"),
                "t": aircraft_info.get("icao"),
                "lat": lat,
                "lon": lon,
                "alt_baro": alt_baro,
                "gs": gs,
                "airline_name": airline_info.get("name"),
                "airline_icao": airline_info.get("icao"),
                "airline_iata": airline_info.get("iata"),
                "flight_number": flight_info.get("number"),
                "flight_icao": flight_info.get("icao"),
                "flight_iata": flight_info.get("iata"),
            }
        )

    return output


def fetch_flights() -> List[Dict[str, Any]]:
    print("Fetching active SIN arrival flights from Aviationstack...")

    params = {
        "access_key": AVIATIONSTACK_ACCESS_KEY,
        "limit": 100,
        "arr_iata": "SIN",
        "flight_status": "active",
    }

    try:
        response = session.get(AVIATIONSTACK_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            print(f"Aviationstack API error: {data['error']}")
            return []

        flights = extract_aviationstack_flights(data)
        print(f"Fetched {len(flights)} active SIN arrival flights from Aviationstack.")
        return flights

    except requests.exceptions.Timeout:
        print("Aviationstack request timed out.")
        return []
    except requests.exceptions.RequestException as exc:
        print(f"Aviationstack request failed: {exc}")
        return []
    except ValueError as exc:
        print(f"Failed to parse Aviationstack JSON response: {exc}")
        return []


def is_inside_alert_zone(lat: Any, lon: Any) -> bool:
    try:
        lat_value = float(lat)
        lon_value = float(lon)
    except (TypeError, ValueError):
        return False

    return (
        ALERT_MIN_LAT <= lat_value <= ALERT_MAX_LAT
        and ALERT_MIN_LON <= lon_value <= ALERT_MAX_LON
    )


def normalize_callsign(flight: Dict[str, Any]) -> Optional[str]:
    callsign = flight.get("flight")
    if not callsign:
        return None
    callsign = str(callsign).strip().upper()
    return callsign or None


def normalize_registration(flight: Dict[str, Any]) -> Optional[str]:
    registration = flight.get("r")
    if not registration:
        return None
    registration = str(registration).strip().upper()
    return registration or None


def normalize_aircraft_type(flight: Dict[str, Any]) -> Optional[str]:
    aircraft_type = flight.get("t")
    if not aircraft_type:
        return None
    aircraft_type = str(aircraft_type).strip().upper()
    return aircraft_type or None


def normalize_altitude(flight: Dict[str, Any]) -> Optional[int]:
    altitude = flight.get("alt_baro")
    if altitude is None:
        return None
    if isinstance(altitude, str) and altitude.lower() in {"ground", "gnd"}:
        return 0
    try:
        return int(float(altitude))
    except (TypeError, ValueError):
        return None


def normalize_ground_speed(flight: Dict[str, Any]) -> Optional[float]:
    groundspeed = flight.get("gs")
    if groundspeed is None:
        return None
    try:
        value = float(groundspeed)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def infer_airline(callsign: Optional[str]) -> Optional[str]:
    if not callsign or len(callsign) < 2:
        return None

    prefix3 = "".join(ch for ch in callsign[:3] if ch.isalpha()).upper()
    if prefix3 in AIRLINE_OPTIONS:
        return prefix3

    prefix2 = callsign[:2].upper()
    return AIRLINE_IATA_TO_ICAO.get(prefix2)


def normalize_airline_code(flight: Dict[str, Any]) -> Optional[str]:
    airline_icao = flight.get("airline_icao")
    if airline_icao:
        airline_icao = str(airline_icao).strip().upper()
        if airline_icao in AIRLINE_OPTIONS:
            return airline_icao

    airline_iata = flight.get("airline_iata")
    if airline_iata:
        airline_iata = str(airline_iata).strip().upper()
        if airline_iata in AIRLINE_IATA_TO_ICAO:
            return AIRLINE_IATA_TO_ICAO[airline_iata]

    return infer_airline(normalize_callsign(flight))


def normalize_airline_name(flight: Dict[str, Any]) -> str:
    airline_name = flight.get("airline_name")
    if airline_name:
        airline_name = str(airline_name).strip()
        if airline_name:
            return airline_name

    airline_code = normalize_airline_code(flight)
    if airline_code:
        return AIRLINE_OPTIONS.get(airline_code, airline_code)

    return "Unknown"


# =========================
# FR24 livery helpers
# =========================

def apply_fr24_rate_limit() -> None:
    global last_fr24_request_time

    elapsed = time.time() - last_fr24_request_time
    if elapsed < FR24_MIN_INTERVAL_SECONDS:
        time.sleep(FR24_MIN_INTERVAL_SECONDS - elapsed)

    last_fr24_request_time = time.time()


def has_special_livery(registration: Optional[str]) -> List[str]:
    if not registration:
        return []

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
        matched = [keyword for keyword in LIVERY_KEYWORDS if keyword in page_text]
        livery_cache[registration] = matched
        print(f"Livery result for {registration}: {matched}")
        return matched
    except requests.exceptions.Timeout:
        print(f"FR24 request timed out for {registration}.")
    except requests.exceptions.RequestException as exc:
        print(f"FR24 request failed for {registration}: {exc}")
    except Exception as exc:
        print(f"FR24 parsing failed for {registration}: {exc}")

    livery_cache[registration] = []
    return []


# =========================
# Matching logic
# =========================

def airline_matches(preferences: Dict[str, Any], flight: Dict[str, Any]) -> bool:
    selected_airlines = preferences.get("airlines", [])
    if not selected_airlines:
        return True
    airline_code = normalize_airline_code(flight)
    return airline_code in set(selected_airlines)


def trim_seen_alerts() -> None:
    for chat_key, alerts in list(seen_alerts.items()):
        if len(alerts) > 500:
            print(f"seen_alerts for chat {chat_key} exceeded 500 entries. Clearing set.")
            seen_alerts[chat_key] = set()


def process_flights(flights: List[Dict[str, Any]]) -> int:
    candidates = 0
    alerts_sent = 0

    for flight in flights:
        lat = flight.get("lat")
        lon = flight.get("lon")

        if not is_inside_alert_zone(lat, lon):
            continue

        callsign = normalize_callsign(flight)
        if not callsign:
            continue

        candidates += 1

        aircraft_type = normalize_aircraft_type(flight) or "UNKNOWN"
        registration = normalize_registration(flight) or "UNKNOWN"
        altitude_ft = normalize_altitude(flight)
        ground_speed_knots = normalize_ground_speed(flight)
        airline_code = normalize_airline_code(flight)
        airline_name = normalize_airline_name(flight)

        print(
            f"Candidate flight: callsign={callsign}, type={aircraft_type}, reg={registration}, "
            f"lat={lat}, lon={lon}, altitude={altitude_ft}, gs={ground_speed_knots}, airline={airline_name}"
        )

        widebody_match = aircraft_type in RARE_AIRCRAFT_TYPES
        livery_matches: List[str] = []

        if not widebody_match and altitude_ft is not None and altitude_ft < LIVERY_ALTITUDE_THRESHOLD_FT:
            livery_matches = has_special_livery(normalize_registration(flight))

        for chat_key, preferences in user_preferences.items():
            if not airline_matches(preferences, flight):
                continue

            reason = None
            selected_types = set(preferences.get("rare_types", []))
            aircraft_mode = preferences.get("aircraft_mode", "selected")

            if aircraft_mode == "any" and aircraft_type != "UNKNOWN":
                reason = "Any Aircraft Type"
            elif widebody_match and aircraft_type in selected_types:
                reason = "Widebody"

            if not reason and livery_matches:
                livery_mode = preferences.get("livery_mode", "any")
                if livery_mode == "any":
                    reason = "Special Livery"
                elif livery_mode == "specific":
                    selected_keywords = set(preferences.get("livery_keywords", []))
                    matched_selected = [kw for kw in livery_matches if kw in selected_keywords]
                    if matched_selected:
                        livery_matches = matched_selected
                        reason = "Special Livery"

            if (
                not reason
                and preferences.get("alert_selected_airlines", False)
                and preferences.get("airlines")
                and airline_code in set(preferences.get("airlines", []))
            ):
                reason = "Selected Airline"

            if not reason:
                continue

            chat_alerts = get_seen_alerts(int(chat_key))
            dedupe_key = f"{callsign}:{reason}"
            if dedupe_key in chat_alerts:
                continue

            message = (
                "✈️ Rare Aircraft Detected!\n"
                f"Callsign: {callsign}\n"
                f"Type: {aircraft_type}\n"
                f"Registration: {registration}\n"
                f"Airline: {airline_name}\n"
                f"Latitude: {lat}\n"
                f"Longitude: {lon}\n"
                f"Reason: {reason}"
            )

            if reason == "Special Livery":
                message += f"\nLivery Match: {', '.join(livery_matches)}"

            print(f"Sending alert to chat {chat_key} for {callsign} ({reason})")
            send_telegram(int(chat_key), message)
            chat_alerts.add(dedupe_key)
            alerts_sent += 1

    print(
        "Flights inside alert zone: "
        f"{candidates} "
        f"(lat {ALERT_MIN_LAT}-{ALERT_MAX_LAT}, lon {ALERT_MIN_LON}-{ALERT_MAX_LON})"
    )
    trim_seen_alerts()
    return alerts_sent


def run_scan() -> Tuple[int, int]:
    flights = fetch_flights()
    if not flights:
        print("No flight data received for this scan.")
        bot_state["last_scan_time"] = time.time()
        bot_state["scan_count"] = int(bot_state.get("scan_count", 0)) + 1
        save_bot_state()
        return 0, 0

    alerts_sent = process_flights(flights)
    bot_state["last_scan_time"] = time.time()
    bot_state["scan_count"] = int(bot_state.get("scan_count", 0)) + 1
    save_bot_state()
    return len(flights), alerts_sent


# =========================
# Telegram command handling
# =========================

def handle_start(chat_id: int) -> None:
    get_user_preferences(chat_id)
    send_telegram(chat_id, build_start_message(), build_main_menu())


def handle_settings(chat_id: int) -> None:
    get_user_preferences(chat_id)
    send_telegram(chat_id, build_settings_summary(chat_id), build_main_menu())


def handle_status(chat_id: int) -> None:
    scanning_enabled = bool(bot_state.get("scanning_enabled", False))
    last_scan_time = float(bot_state.get("last_scan_time", 0.0))
    scan_count = int(bot_state.get("scan_count", 0))

    if last_scan_time > 0:
        last_scan_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_scan_time))
    else:
        last_scan_text = "Never"

    message = (
        "Bot Status\n\n"
        f"Scanning: {'ON' if scanning_enabled else 'OFF'}\n"
        f"Data source: aviationstack\n"
        f"Interval: {int(bot_state.get('scan_interval_seconds', SCAN_INTERVAL_SECONDS))} seconds\n"
        f"Alert zone: lat {ALERT_MIN_LAT}-{ALERT_MAX_LAT}, lon {ALERT_MIN_LON}-{ALERT_MAX_LON}\n"
        f"Scans run: {scan_count}\n"
        f"Last scan: {last_scan_text}"
    )
    send_telegram(chat_id, message)


def handle_scanon(chat_id: int) -> None:
    global next_scan_time
    bot_state["scanning_enabled"] = True
    save_bot_state()
    next_scan_time = time.time()
    send_telegram(chat_id, "Scanning enabled. The bot will now run scheduled scans.")


def handle_scanoff(chat_id: int) -> None:
    bot_state["scanning_enabled"] = False
    save_bot_state()
    send_telegram(chat_id, "Scanning disabled. The bot will stay idle until you turn it on again.")


def handle_scanonce(chat_id: int) -> None:
    send_telegram(chat_id, "Running one scan now...")
    flights_count, alerts_sent = run_scan()
    send_telegram(chat_id, f"Scan complete.\nFlights fetched: {flights_count}\nAlerts sent: {alerts_sent}")


def handle_message(update: Dict[str, Any]) -> None:
    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip().lower()

    if not chat_id or not text:
        return

    if text == "/start":
        handle_start(chat_id)
    elif text == "/settings":
        handle_settings(chat_id)
    elif text == "/status":
        handle_status(chat_id)
    elif text == "/scanonce":
        handle_scanonce(chat_id)
    elif text == "/scanon":
        handle_scanon(chat_id)
    elif text == "/scanoff":
        handle_scanoff(chat_id)
    else:
        send_telegram(chat_id, build_start_message(), build_main_menu())


def handle_callback(update: Dict[str, Any]) -> None:
    callback = update.get("callback_query", {})
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not callback_id or not chat_id or not message_id:
        return

    prefs = get_user_preferences(chat_id)

    try:
        parts = data.split(":", 2)
        action = parts[0]

        if action == "menu":
            menu_name = parts[1]
            text, keyboard = get_menu_text(menu_name, chat_id)
            edit_telegram_message(chat_id, message_id, text, keyboard)
            answer_callback_query(callback_id)
            return

        if action == "toggle":
            category = parts[1]
            value = parts[2]

            if category == "aircraft":
                selected = set(prefs.get("rare_types", []))
                if value in selected:
                    selected.remove(value)
                else:
                    selected.add(value)
                prefs["rare_types"] = sorted(list(selected))
                prefs["aircraft_mode"] = "selected"
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose the rare aircraft types you want alerts for.",
                    build_aircraft_menu(chat_id),
                )
                answer_callback_query(callback_id, "Aircraft preferences updated.")
                return

            if category == "keyword":
                selected = set(prefs.get("livery_keywords", []))
                if value in selected:
                    selected.remove(value)
                else:
                    selected.add(value)
                prefs["livery_keywords"] = sorted(list(selected))
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose the livery keywords you want when 'Specific Livery' mode is enabled.",
                    build_keyword_menu(chat_id),
                )
                answer_callback_query(callback_id, "Livery keyword preferences updated.")
                return

            if category == "airline":
                selected = set(prefs.get("airlines", []))
                if value in selected:
                    selected.remove(value)
                else:
                    selected.add(value)
                prefs["airlines"] = sorted(list(selected))
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose which airlines you want alerts for. Select none for any airline.",
                    build_airline_menu(chat_id),
                )
                answer_callback_query(callback_id, "Airline preferences updated.")
                return

            if category == "airline_mode" and value == "selected":
                prefs["alert_selected_airlines"] = not prefs.get("alert_selected_airlines", False)
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose which airlines you want alerts for. Select none for any airline.",
                    build_airline_menu(chat_id),
                )
                answer_callback_query(callback_id, "Airline-only alert mode updated.")
                return

        if action == "set":
            category = parts[1]
            value = parts[2]

            if category == "aircraft_mode" and value == "any":
                prefs["aircraft_mode"] = "any"
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose the rare aircraft types you want alerts for.",
                    build_aircraft_menu(chat_id),
                )
                answer_callback_query(callback_id, "Aircraft mode set to any.")
                return

            if category == "livery":
                prefs["livery_mode"] = value
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose whether you want livery alerts, and how specific they should be.",
                    build_livery_menu(chat_id),
                )
                answer_callback_query(callback_id, "Livery mode updated.")
                return

            if category == "airline" and value == "any":
                prefs["airlines"] = []
                save_preferences()
                edit_telegram_message(
                    chat_id,
                    message_id,
                    "Choose which airlines you want alerts for. Select none for any airline.",
                    build_airline_menu(chat_id),
                )
                answer_callback_query(callback_id, "Airline filter set to any.")
                return

        if action == "action" and parts[1] == "reset":
            user_preferences[str(chat_id)] = default_preferences()
            save_preferences()
            edit_telegram_message(
                chat_id,
                message_id,
                build_settings_summary(chat_id),
                build_main_menu(),
            )
            answer_callback_query(callback_id, "Preferences reset.")
            return

    except Exception as exc:
        print(f"Failed to handle callback '{data}': {exc}")

    answer_callback_query(callback_id, "Unable to update settings.")


def process_telegram_updates() -> None:
    updates = fetch_telegram_updates()
    for update in updates:
        if "message" in update:
            handle_message(update)
        elif "callback_query" in update:
            handle_callback(update)


# =========================
# Main loop
# =========================

def main() -> None:
    global user_preferences, bot_state, next_scan_time

    user_preferences = load_preferences()
    bot_state = load_bot_state()
    next_scan_time = time.time() + int(bot_state.get("scan_interval_seconds", SCAN_INTERVAL_SECONDS))

    print("Starting Singapore rare flight tracker bot (box mode)...")
    print(f"Loaded {len(user_preferences)} user preference profiles.")
    print(f"Scanning enabled: {bot_state.get('scanning_enabled', False)}")
    print(f"Alert zone: lat {ALERT_MIN_LAT}-{ALERT_MAX_LAT}, lon {ALERT_MIN_LON}-{ALERT_MAX_LON}")

    while True:
        try:
            process_telegram_updates()

            now = time.time()
            if bot_state.get("scanning_enabled", False) and now >= next_scan_time:
                print("Running scheduled scan...")
                run_scan()
                next_scan_time = now + int(bot_state.get("scan_interval_seconds", SCAN_INTERVAL_SECONDS))

        except Exception as exc:
            print(f"Unexpected error in main loop: {exc}")

        time.sleep(BOT_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
