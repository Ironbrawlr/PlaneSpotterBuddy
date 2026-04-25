# Singapore Flight Tracker Bot

A Python Telegram bot that watches flights arriving into Singapore using Aviationstack, filters aircraft inside a configurable Singapore alert zone, and sends notifications based on your preferences.

## Features

- Monitors active flights arriving at `SIN`
- Filters flights by latitude/longitude alert zone
- Supports Telegram preferences for:
  - aircraft types
  - airlines
  - livery alerts
- Supports airline-only alerts
- Supports an `Any` aircraft-type mode
- Manual scan controls so you do not waste API quota
- Caches livery lookups

## Main Script

Use:

```bash
flight_tracker_aviationstack_box.py
```

## Requirements

- Python 3.9+
- `requests`
- `beautifulsoup4`
- An Aviationstack API key
- A Telegram bot token

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install requests beautifulsoup4
```

Set environment variables:

```bash
export AVIATIONSTACK_ACCESS_KEY="YOUR_AVIATIONSTACK_KEY"
export TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
```

Optional alert-zone overrides:

```bash
export ALERT_MIN_LAT="0.8"
export ALERT_MAX_LAT="2.2"
export ALERT_MIN_LON="103.3"
export ALERT_MAX_LON="104.7"
```

Run the bot:

```bash
python3 flight_tracker_aviationstack_box.py
```

## Telegram Commands

- `/start` shows help and available commands
- `/settings` opens alert preferences
- `/status` shows scan status
- `/scanonce` runs one scan immediately
- `/scanon` enables scheduled scanning
- `/scanoff` disables scheduled scanning

## Alert Logic

The bot fetches active `SIN` arrivals from Aviationstack and checks whether each flight is inside the configured alert zone.

Alerts can be triggered by:

- selected rare aircraft types
- `Any` aircraft-type mode
- selected airlines with airline-only alerts enabled
- optional livery matches

## Notes

- Aviationstack data may be incomplete or stale compared with flight-tracking websites.
- Livery checks use Flightradar24 aircraft pages and may slow scans when enabled.
- If you want faster scans, set livery alerts to `Off`.

## Files

- `flight_tracker_aviationstack_box.py` — main bot
- `user_preferences_box.json` — saved Telegram user preferences
- `bot_state_box.json` — saved bot scan state
