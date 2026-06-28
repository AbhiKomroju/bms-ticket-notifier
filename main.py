"""
BMS Multi-Ticket Checker — CI/Headless mode for GitHub Actions.
Supports multiple comma-separated URLs and multiple theatres.
"""

import os
import re
import sys
import json
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests
from curl_cffi import requests as browser

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — set via GitHub Secrets / Variables
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    # Can now be a comma-separated list of multiple BookMyShow URLs
    "url": os.getenv("BMS_URL", ""),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # comma-separated theatre names (e.g., "AMB,Prasads")
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_FILE = "bms_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"), 
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list[CatInfo] = field(default_factory=list)

@dataclass
class DateInfo:
    date_code: str
    status: str


def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


API_URL = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"


def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash, movie_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": movie_url,
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "lat": lat, "lon": lon,
    }
    try:
        resp = browser.get(API_URL, headers=headers, params=params, timeout=15, impersonate="chrome")
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except Exception as e:
        print(f"  Request failed: {e}")
    return None


def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_shows(data):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(sa.get("showDateCode", "") or sa.get("dateCode", "")).strip()
                    if not date_code and re.match(r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "") or sa.get("attributes", "")),
                    )
                    for cat in sa.get("categories", []):
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=str(cat.get("availStatus", "")),
                        ))
                    shows.append(show)
    return shows


def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    kws = [k.strip().lower() for k in theatre_filter.split(",") if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",") if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",") if d.strip()) if date_codes else set()

    for s in shows:
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
                continue
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue
        result.append(s)
    return result


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_to_state(global_state, event_code, shows, dates):
    """Appends data for a specific movie into the centralized state tracking dictionary."""
    if "shows" not in global_state:
        global_state["shows"] = {}
    if "dates" not in global_state:
        global_state["dates"] = {}

    for s in shows:
        for c in s.categories:
            # Prepend event_code to ensure key uniqueness across multiple movies
            key = f"{event_code}|{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            global_state["shows"][key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
                "event": event_code
            }
            
    for d in dates:
        date_key = f"{event_code}|{d.date_code}"
        global_state["dates"][date_key] = d.status


def detect_movie_changes(old_state, new_state, event_code):
    changes = []
    
    # Filter dates and shows specific to this movie context
    old_dates = {k.split("|")[1]: v for k, v in old_state.get("dates", {}).items() if k.startswith(f"{event_code}|")}
    new_dates = {k.split("|")[1]: v for k, v in new_state.get("dates", {}).items() if k.startswith(f"{event_code}|")}
    
    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)
        if old_status == "NOT_OPEN" and status in ("BOOKABLE", "AVAILABLE"):
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    old_shows = {k: v for k, v in old_state.get("shows", {}).items() if v.get("event") == event_code}
    new_shows = {k: v for k, v in new_state.get("shows", {}).items() if v.get("event") == event_code}

    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        changes.append(f"🆕 NEW SHOW: {s['venue']} {s['time']} [{s['date']}] — {s['cat']} ₹{s['price']}")

    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            _, ico = AVAIL_STATUS_MAP.get(new_s["status"], ("UNKNOWN", "⚪"))
            changes.append(f"{ico} SEATS AVAILABLE AGAIN: {new_s['venue']} {new_s['time']} [{new_s['date']}]")

    return changes


def send_telegram_message(changes, movie_info, movie_url):
    bot_token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()

    if not bot_token or not chat_id:
        print("  ⚠️  Skipping Telegram — Token or Chat ID not configured.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")

    message = f"🚨 <b>BMS Alert: {escape(movie_name)}</b>\n"
    message += f"🕒 <i>{now_str}</i>\n\n"

    message += "<b>Changes Detected:</b>\n"
    for c in changes[:15]:
        message += f"• {escape(c)}\n"
    if len(changes) > 15:
        message += f"• ...and {len(changes)-15} more.\n"
    message += "\n"

    message += f"🔗 <a href='{movie_url}'>Book Tickets Here</a>"

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            print(f"  ✅ Telegram alert sent for {movie_name}!")
        else:
            print(f"  ❌ Telegram API Error {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"  ❌ Telegram request failed: {e}")


def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Multi-Ticket Checker Active")

    # Split the input string into separate URLs
    urls = [u.strip() for u in CONFIG["url"].split(",") if u.strip()]
    if not urls:
        print("  ❌ No URLs found in BMS_URL environment variable.")
        sys.exit(1)

    old_state = load_state()
    new_state = {"shows": {}, "dates": {}}

    # Preserve historical states for other tracking entries not currently called in this execution list
    if old_state:
        new_state["shows"].update(old_state.get("shows", {}))
        new_state["dates"].update(old_state.get("dates", {}))

    for movie_url in urls:
        print(f"\nProcessing Movie Link: {movie_url}")
        parsed = parse_bms_url(movie_url)
        event_code = parsed["event_code"]
        region_slug = parsed["region_slug"]
        url_date = parsed.get("date_code", "")

        if not event_code or not region_slug:
            print(f"  ⚠️ Skipping invalid URL configuration layout.")
            continue

        region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

        raw_dates = CONFIG["dates"].strip()
        if raw_dates:
            date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
        elif url_date:
            date_list = [url_date]
        else:
            date_list = [""]

        movie_shows = []
        movie_dates = []
        movie_info = {"name": "Unknown", "language": ""}

        for dc in date_list:
            data = fetch_bms(event_code, dc, region_code, region_slug_r, lat, lon, geohash, movie_url)
            if not data:
                continue

            if movie_info["name"] == "Unknown":
                movie_info = parse_movie_info(data)

            movie_dates.extend(parse_dates(data))
            movie_shows.extend(parse_shows(data))

        if not movie_shows:
            print("  ❌ No current showtimes found for this title.")
            continue

        print(f"  🎬 {movie_info['name']} ({movie_info['language']})")

        filtered = filter_shows(movie_shows, CONFIG["theatre"], CONFIG["time_period"], CONFIG["dates"])
        print(f"  📊 {len(filtered)} showtime(s) matching criteria filters")

        # Append data to global runtime collection
        append_to_state(new_state, event_code, filtered, movie_dates)

        # Check for modifications against the last execution cycle
        if old_state:
            changes = detect_movie_changes(old_state, new_state, event_code)
            if changes:
                print(f"  ⚡ {len(changes)} update(s) caught!")
                send_telegram_message(changes, movie_info, movie_url)
            else:
                print("  ✅ No structural updates caught since the last run cycle.")

    # Commit unified tracking data state at completion
    save_state(new_state)
    print("\nBatch Operations Finished.")


if __name__ == "__main__":
    main()