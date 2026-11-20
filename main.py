import os
import re
import sys
import json
import random
import time
from html import escape
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests
from curl_cffi import requests as browser

# Load variables from a local .env file if python-dotenv is installed and a
# .env file exists next to this script. This only affects local/VS Code runs
# — in GitHub Actions, secrets/vars are already injected as real env vars,
# and load_dotenv() silently does nothing if no .env file is present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — set via GitHub Secrets / Variables (or a local .env file)
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    # Can now be a comma-separated list of multiple BookMyShow URLs
    "url": os.getenv("BMS_URL", ""),
    "dates": os.getenv("BMS_DATES", ""),          # comma-separated YYYYMMDD, empty = from URL
    "theatre": os.getenv("BMS_THEATRE", ""),       # comma-separated theatre names (e.g., "AMB,Prasads")
    "screen": os.getenv("BMS_SCREEN", ""),         # comma-separated screen/audi names (e.g., "Audi 5,IMAX")
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
}
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = "bms_state.json"
SCHEDULE_FILE = "bms_schedule.json"
DEBUG = os.getenv("BMS_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")
FORCE_RUN = os.getenv("BMS_FORCE_RUN", "0").strip().lower() in ("1", "true", "yes", "on")


def _int_env(key, default):
    """Reads an int env var, tolerating unset OR empty-string values (GitHub
    Actions passes '' for an unset `vars.X`, which would otherwise crash a
    bare int(os.getenv(...)) call)."""
    val = os.getenv(key, "").strip()
    return int(val) if val else default


# Randomized check interval, in minutes. Tighten this for more aggressive
# checking (e.g. 5-10) or widen it to look less like an automated poll.
MIN_INTERVAL_MIN = _int_env("BMS_MIN_INTERVAL", 20)
MAX_INTERVAL_MIN = _int_env("BMS_MAX_INTERVAL", 40)


def dbg(*args):
    """Print only when BMS_DEBUG=1. Use this to capture logs to share back."""
    if DEBUG:
        print("  [DEBUG]", *args)


# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}
# NOTE: if BMS_DEBUG output shows styleId values that AREN'T listed here,
# add them to this map (that's very likely the root cause of dates not
# flipping to BOOKABLE — BMS may use different styleId strings than these).
DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}
OPEN_STATUSES = {"BOOKABLE", "AVAILABLE"}
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
    raw_style: str = ""   # kept for debug visibility


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


def split_csv_values(raw_value):
    """Split a comma-separated environment variable into non-empty values."""
    return [value.strip() for value in (raw_value or "").split(",") if value.strip()]


def display_date(date_code):
    """Convert a BMS YYYYMMDD date code into a readable date."""
    if not date_code:
        return "Not specified"
    try:
        return datetime.strptime(str(date_code), "%Y%m%d").strftime("%d %b %Y")
    except (TypeError, ValueError):
        return str(date_code)


def get_movie_name_from_url(movie_url):
    """Get a readable movie name from the /movies/<city>/<movie>/ URL path."""
    try:
        parts = urlparse(movie_url).path.strip("/").split("/")
        if "movies" in parts:
            idx = parts.index("movies")
            if len(parts) > idx + 2:
                return parts[idx + 2].replace("-", " ").title()
    except Exception:
        pass
    return "Unknown Movie"


def get_dates_for_movie(url_date):
    """Resolve the dates that will be requested for a movie URL."""
    configured_dates = split_csv_values(CONFIG["dates"])
    if configured_dates:
        return configured_dates, "BMS_DATES variable"
    if url_date:
        return [url_date], "date in BMS_URL"
    return [""], "BMS default (no date supplied)"


def log_filter_configuration(urls):
    """Print all configured filters and the movie URLs they apply to."""
    theatre_keywords = split_csv_values(CONFIG["theatre"])
    screen_keywords = split_csv_values(CONFIG["screen"])
    time_periods = split_csv_values(CONFIG["time_period"])
    configured_dates = split_csv_values(CONFIG["dates"])

    print("\n" + "=" * 64)
    print("BMS CONFIGURATION — FILTERS THAT WILL BE USED")
    print("=" * 64)
    print("Theatre keywords : " + (", ".join(theatre_keywords) if theatre_keywords else "ALL THEATRES"))
    print("Screen keywords  : " + (", ".join(screen_keywords) if screen_keywords else "ALL SCREENS"))
    if configured_dates:
        formatted_dates = [f"{date} ({display_date(date)})" for date in configured_dates]
        print("Date filters     : " + ", ".join(formatted_dates))
    else:
        print("Date filters     : From each movie URL (or BMS default if URL has no date)")
    print("Time filters     : " + (", ".join(time_periods) if time_periods else "ALL TIMES"))
    print("Filter logic     : OR within each filter; AND between theatre/screen/date/time")
    print("Matching         : Case-insensitive substring matching for theatre and screen keywords")

    print("\nMovies configured:")
    for index, movie_url in enumerate(urls, start=1):
        parsed = parse_bms_url(movie_url)
        movie_name = get_movie_name_from_url(movie_url)
        movie_dates, date_source = get_dates_for_movie(parsed.get("date_code", ""))
        date_labels = ", ".join(
            f"{date or 'default'} ({display_date(date)})" for date in movie_dates
        )
        print(f"  {index}. Movie       : {movie_name}")
        print(f"     City          : {parsed.get('region_slug') or 'Unknown'}")
        print(f"     Event code    : {parsed.get('event_code') or 'Invalid / missing'}")
        print(f"     Dates to query: {date_labels} [{date_source}]")
    print("=" * 64 + "\n")


def log_returned_inventory(shows, movie_dates, theatre_keywords, screen_keywords):
    """Log BMS-returned theatres/screens and show which keywords match them."""
    returned_theatres = sorted({show.venue_name for show in shows if show.venue_name})
    returned_screens = sorted({show.screen_attr for show in shows if show.screen_attr})

    print("\nBMS RESPONSE INVENTORY")
    print("-" * 48)
    print(f"Theatres returned ({len(returned_theatres)}):")
    if returned_theatres:
        for theatre in returned_theatres:
            matched_keywords = [
                keyword for keyword in theatre_keywords
                if keyword.lower() in theatre.lower()
            ]
            if matched_keywords:
                match_note = f" | matched keyword(s): {', '.join(matched_keywords)}"
            elif theatre_keywords:
                match_note = " | no configured theatre keyword matched"
            else:
                match_note = " | no theatre filter configured; eligible"
            print(f"  • {theatre}{match_note}")
    else:
        print("  • No theatres were found in the BMS response.")

    print("\nConfigured theatre keyword matches:")
    if theatre_keywords:
        for keyword in theatre_keywords:
            matching_theatres = [
                theatre for theatre in returned_theatres
                if keyword.lower() in theatre.lower()
            ]
            print(f"  • {keyword!r}: {len(matching_theatres)} theatre(s) matched")
            for theatre in matching_theatres:
                print(f"      - {theatre}")
    else:
        print("  • No theatre filter configured; all theatres are eligible.")

    print(f"\nScreen attributes returned ({len(returned_screens)}):")
    if returned_screens:
        for screen in returned_screens:
            matched_keywords = [
                keyword for keyword in screen_keywords
                if keyword.lower() in screen.lower()
            ]
            if matched_keywords:
                match_note = f" | matched keyword(s): {', '.join(matched_keywords)}"
            elif screen_keywords:
                match_note = " | no configured screen keyword matched this attribute"
            else:
                match_note = " | no screen filter configured; eligible"
            print(f"  • {screen}{match_note}")
    else:
        print("  • No screen attributes were populated by BMS.")

    print("\nConfigured screen keyword matches (screen attribute OR show time):")
    if screen_keywords:
        for keyword in screen_keywords:
            matching_shows = [
                show for show in shows
                if keyword.lower() in f"{show.screen_attr} {show.time}".lower()
            ]
            print(f"  • {keyword!r}: {len(matching_shows)} showtime(s) matched")
            for show in matching_shows[:10]:
                print(
                    f"      - {show.venue_name} | {show.screen_attr or 'No screen attribute'}"
                    f" | {show.time} | {show.date_code or 'Date unknown'}"
                )
            if len(matching_shows) > 10:
                print(f"      - ...and {len(matching_shows) - 10} more")
    else:
        print("  • No screen filter configured; all screens are eligible.")

    print("\nDate information returned by BMS:")
    if movie_dates:
        unique_dates = {}
        for item in movie_dates:
            unique_dates[item.date_code] = item
        for date_code in sorted(unique_dates):
            item = unique_dates[date_code]
            print(
                f"  • {date_code} ({display_date(date_code)})"
                f" | status: {item.status} | BMS style: {item.raw_style or 'not supplied'}"
            )
    else:
        print("  • No date-selector information was found in the response.")
    print("-" * 48)


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


API_URL = "https://in.bookmyshow.com/api/movies-data/v4/showtimes-by-event/primary-dynamic"


def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash, movie_url, max_retries=2):
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
    for attempt in range(max_retries + 1):
        try:
            # Small random pre-request delay so requests don't land on a
            # perfectly predictable clock offset every single run.
            time.sleep(random.uniform(1, 4))
            resp = browser.get(API_URL, headers=headers, params=params, timeout=15, impersonate="chrome")
            dbg(f"fetch_bms attempt={attempt + 1}/{max_retries + 1} status={resp.status_code} "
                f"event={event_code} date={date_code!r}")
            if resp.status_code == 200:
                data = resp.json()
                if DEBUG:
                    # Dump full response so you can capture real field names/styleIds.
                    # Truncated to keep CI logs sane; raise the slice if you need more.
                    dumped = json.dumps(data, indent=2)[:20000]
                    print("  [DEBUG] ---- RAW API RESPONSE (truncated to 20k chars) ----")
                    print(dumped)
                    print("  [DEBUG] ---- END RAW API RESPONSE ----")
                return data
            retry_note = " (retrying...)" if attempt < max_retries else " (giving up)"
            print(f"  HTTP {resp.status_code}{retry_note}")
        except Exception as e:
            retry_note = " (retrying...)" if attempt < max_retries else " (giving up)"
            print(f"  Request failed: {e}{retry_note}")
        if attempt < max_retries:
            backoff = (2 ** attempt) + random.uniform(0, 1.5)
            dbg(f"backing off {backoff:.1f}s before retry")
            time.sleep(backoff)
    return None


def parse_movie_info(data, movie_url):
    info = {"name": "Unknown Movie", "language": ""}

    # Try to grab the language/format (e.g., "English • 2D") if it exists
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    # Fallback to bottom sheet for title if available
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    # ALWAYS rip the movie title directly from the URL slug as a priority
    if movie_url:
        try:
            parts = urlparse(movie_url).path.strip("/").split("/")
            if "movies" in parts:
                idx = parts.index("movies")
                if len(parts) > idx + 2:
                    raw_slug = parts[idx + 2]  # Extracts 'spiderman-brand-new-day'
                    info["name"] = raw_slug.replace("-", " ").title()
        except Exception:
            pass
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
                status = DATE_STYLE_MAP.get(style, "UNKNOWN")
                date_code = item.get("id", "")
                if DEBUG:
                    # Show the raw text labels (day/weekday) alongside styleId
                    # so you can confirm which date_code corresponds to which
                    # visible date, and see any styleId not yet in the map.
                    labels = []
                    for t in texts:
                        if isinstance(t, dict) and "text" in t:
                            labels.append(t["text"])
                    dbg(f"date item id={date_code!r} styleId={style!r} -> {status} labels={labels}")
                dates.append(DateInfo(date_code=date_code, status=status, raw_style=style))
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
                    screen_attr = (st.get("screenAttr", "") or sa.get("attributes", ""))
                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=screen_attr,
                    )
                    for cat in sa.get("categories", []):
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=str(cat.get("availStatus", "")),
                        ))
                    if DEBUG:
                        dbg(f"show venue={vname!r} time={st.get('title','')!r} "
                            f"screen_attr={screen_attr!r} date={date_code} "
                            f"cats={[c.name for c in show.categories]}")
                    shows.append(show)
    return shows


def filter_shows(shows, theatre_filter, screen_filter, time_periods, date_codes):
    result = []
    theatre_kws = [k.strip().lower() for k in theatre_filter.split(",") if k.strip()] if theatre_filter else []
    screen_kws = [k.strip().lower() for k in screen_filter.split(",") if k.strip()] if screen_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",") if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",") if d.strip()) if date_codes else set()

    for s in shows:
        if theatre_kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in theatre_kws):
                continue
        if screen_kws:
            # Match against screen_attr primarily; fall back to the show's
            # time-slot title, since some BMS payloads put the screen/audi
            # name there instead of in screenAttr/attributes.
            haystack = f"{s.screen_attr} {s.time}".lower()
            if not any(k in haystack for k in screen_kws):
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


def load_schedule():
    try:
        with open(SCHEDULE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_schedule(schedule):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)


def append_to_state(global_state, event_code, shows, dates):
    """Appends data for a specific movie into the centralized state tracking dictionary."""
    if "shows" not in global_state:
        global_state["shows"] = {}
    if "dates" not in global_state:
        global_state["dates"] = {}
    for s in shows:
        for c in s.categories:
            key = f"{event_code}|{s.venue_code}|{s.session_id}|{s.date_code}|{c.name}"
            global_state["shows"][key] = {
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
                "screen": s.screen_attr,
                "event": event_code,
            }

    for d in dates:
        date_key = f"{event_code}|{d.date_code}"
        global_state["dates"][date_key] = d.status


def detect_movie_changes(old_state, new_state, event_code):
    changes = []

    old_dates = {k.split("|")[1]: v for k, v in old_state.get("dates", {}).items() if k.startswith(f"{event_code}|")}
    new_dates = {k.split("|")[1]: v for k, v in new_state.get("dates", {}).items() if k.startswith(f"{event_code}|")}

    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)  # None if this date is brand new
        # Fires whenever we flip INTO an open status from anything that
        # wasn't already open — covers None (new date), NOT_OPEN, and
        # UNKNOWN (in case DATE_STYLE_MAP is missing a real styleId).
        if status in OPEN_STATUSES and old_status not in OPEN_STATUSES:
            changes.append(f"📅 NEW DATE OPENED: {dc} (was {old_status!r} -> {status})")

    old_shows = {k: v for k, v in old_state.get("shows", {}).items() if v.get("event") == event_code}
    new_shows = {k: v for k, v in new_state.get("shows", {}).items() if v.get("event") == event_code}
    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        screen_note = f" [{s['screen']}]" if s.get("screen") else ""
        changes.append(f"🆕 NEW SHOW: {s['venue']} {s['time']}{screen_note} [{s['date']}] — {s['cat']} ₹{s['price']}")
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            _, ico = AVAIL_STATUS_MAP.get(new_s["status"], ("UNKNOWN", "⚪"))
            screen_note = f" [{new_s['screen']}]" if new_s.get("screen") else ""
            changes.append(f"{ico} SEATS AVAILABLE AGAIN: {new_s['venue']} {new_s['time']}{screen_note} [{new_s['date']}]")
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
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{now_str}] BMS Multi-Ticket Checker Active (debug={'ON' if DEBUG else 'off'})")

    urls = split_csv_values(CONFIG["url"])
    if not urls:
        print("  ❌ No URLs found in BMS_URL environment variable.")
        sys.exit(1)

    # Log filters before the randomized gate so even skipped GitHub Actions
    # ticks show exactly which configuration is currently deployed.
    log_filter_configuration(urls)

    if MIN_INTERVAL_MIN < 0 or MAX_INTERVAL_MIN < 0 or MIN_INTERVAL_MIN > MAX_INTERVAL_MIN:
        print(
            "  ❌ Invalid randomized interval: "
            f"BMS_MIN_INTERVAL={MIN_INTERVAL_MIN}, BMS_MAX_INTERVAL={MAX_INTERVAL_MIN}. "
            "Both must be non-negative and MIN must be <= MAX."
        )
        sys.exit(1)

    # ── Randomized scheduling gate ──────────────────────────────────────
    # The GitHub Actions cron starts this script on a fixed cadence. This
    # gate decides whether this invocation makes a real BMS request.
    schedule = load_schedule()
    next_allowed_str = schedule.get("next_allowed_run")
    now = datetime.now()
    if next_allowed_str and not FORCE_RUN:
        try:
            next_allowed = datetime.fromisoformat(next_allowed_str)
        except ValueError:
            print(f"  ⚠️ Invalid next_allowed_run value in {SCHEDULE_FILE}: {next_allowed_str!r}")
            next_allowed = now
        if now < next_allowed:
            remaining = (next_allowed - now).total_seconds() / 60
            print(
                f"  ⏭️ Not due yet — next actual BMS check in ~{remaining:.1f} min "
                f"(scheduled for {next_allowed.strftime('%Y-%m-%d %H:%M:%S')}). "
                f"Randomized interval: {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN} min."
            )
            sys.exit(0)
    elif FORCE_RUN:
        print("  ⚡ BMS_FORCE_RUN=1 — bypassing the randomized schedule gate.")

    old_state = load_state()
    new_state = {"shows": {}, "dates": {}}
    if old_state:
        new_state["shows"].update(old_state.get("shows", {}))
        new_state["dates"].update(old_state.get("dates", {}))

    pending_notifications = []

    theatre_keywords = split_csv_values(CONFIG["theatre"])
    screen_keywords = split_csv_values(CONFIG["screen"])

    for movie_index, movie_url in enumerate(urls, start=1):
        parsed = parse_bms_url(movie_url)
        event_code = parsed["event_code"]
        region_slug = parsed["region_slug"]
        url_date = parsed.get("date_code", "")
        movie_name_from_url = get_movie_name_from_url(movie_url)

        print("\n" + "=" * 64)
        print(f"🎬 MOVIE {movie_index}/{len(urls)}: {movie_name_from_url}")
        print(f"🔖 Event code: {event_code or 'Missing'}")
        print(f"📍 City: {region_slug or 'Unknown'}")
        print("=" * 64)

        if not event_code or not region_slug:
            print("  ⚠️ Skipping invalid BMS URL: event code or city could not be parsed.")
            continue

        region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)
        date_list, date_source = get_dates_for_movie(url_date)

        print(f"📅 Dates that will be requested ({date_source}):")
        for date_code in date_list:
            print(f"  • {date_code or 'BMS default'} — {display_date(date_code)}")

        movie_shows = []
        movie_dates = []
        movie_info = {"name": movie_name_from_url, "language": ""}
        successful_date_requests = 0

        for date_index, date_code in enumerate(date_list, start=1):
            print("\n" + "-" * 56)
            print(f"🌐 BMS REQUEST {date_index}/{len(date_list)}")
            print(f"   Movie : {movie_name_from_url}")
            print(f"   Event : {event_code}")
            print(f"   Date  : {date_code or 'BMS default'} ({display_date(date_code)})")
            print("-" * 56)

            data = fetch_bms(
                event_code,
                date_code,
                region_code,
                region_slug_r,
                lat,
                lon,
                geohash,
                movie_url,
            )
            if not data:
                print(f"  ❌ No usable API response for requested date {date_code or 'BMS default'}.")
                continue

            successful_date_requests += 1
            # The title is derived from the URL slug by parse_movie_info;
            # parsing each response also refreshes the language/format label.
            parsed_movie_info = parse_movie_info(data, movie_url)
            if parsed_movie_info.get("name"):
                movie_info["name"] = parsed_movie_info["name"]
            if parsed_movie_info.get("language"):
                movie_info["language"] = parsed_movie_info["language"]

            date_results = parse_dates(data)
            show_results = parse_shows(data)
            movie_dates.extend(date_results)
            movie_shows.extend(show_results)

            print(f"  ✅ API response received for {display_date(date_code)}.")
            print(f"  📊 Showtimes in this response: {len(show_results)}")

            # Dates represented in the response's date selector.
            response_date_codes = sorted({item.date_code for item in date_results if item.date_code})
            print("  📅 Date-selector dates returned by BMS:")
            if response_date_codes:
                for returned_date in response_date_codes:
                    matching_date_info = next(
                        (item for item in date_results if item.date_code == returned_date),
                        None,
                    )
                    status = matching_date_info.status if matching_date_info else "Unknown"
                    print(f"     • {returned_date} ({display_date(returned_date)}) — {status}")
            else:
                print("     • No date-selector entries found.")

            # Also show dates attached to the actual returned showtimes.
            show_date_codes = sorted({show.date_code for show in show_results if show.date_code})
            print("  🎟️ Show dates in returned showtimes:")
            if show_date_codes:
                for returned_date in show_date_codes:
                    print(f"     • {returned_date} ({display_date(returned_date)})")
            else:
                print("     • No showtime date codes found.")

        print("\n" + "=" * 56)
        print(f"📌 MOVIE SUMMARY: {movie_info['name']}")
        print(f"   Successful date requests : {successful_date_requests}/{len(date_list)}")
        print(f"   Total showtimes received : {len(movie_shows)}")
        print("=" * 56)

        log_returned_inventory(
            movie_shows,
            movie_dates,
            theatre_keywords,
            screen_keywords,
        )

        if not movie_shows:
            print("  ❌ No current showtimes found for this movie. Skipping state update for this movie.")
            continue

        # Apply configured filters. The filter implementation is unchanged:
        # OR within theatre/screen/time keyword lists, AND across filter types.
        filtered = filter_shows(
            movie_shows,
            CONFIG["theatre"],
            CONFIG["screen"],
            CONFIG["time_period"],
            CONFIG["dates"],
        )

        print("\nFILTER RESULTS")
        print("-" * 48)
        print(f"📥 Total showtimes received : {len(movie_shows)}")
        print(f"✅ Showtimes matching filters: {len(filtered)}")
        print(f"🚫 Showtimes excluded       : {len(movie_shows) - len(filtered)}")

        if filtered:
            print("\n✅ MATCHING SHOWTIMES:")
            for show in filtered[:100]:
                print(
                    f"  • Theatre: {show.venue_name}"
                    f" | Screen: {show.screen_attr or 'Not supplied by BMS'}"
                    f" | Date: {show.date_code or 'Unknown'} ({display_date(show.date_code)})"
                    f" | Time: {show.time or 'Unknown'}"
                )
            if len(filtered) > 100:
                print(f"  • ...and {len(filtered) - 100} more matching showtimes.")
        else:
            print("⚠️ No showtimes passed all configured filters.")
        print("-" * 48)

        append_to_state(new_state, event_code, filtered, movie_dates)

        if old_state:
            changes = detect_movie_changes(old_state, new_state, event_code)
            if changes:
                print(f"  ⚡ {len(changes)} update(s) detected for {movie_info['name']}.")
                pending_notifications.append((changes, movie_info, movie_url))
            else:
                print("  ✅ No structural updates caught since the last run cycle.")

    save_state(new_state)
    print("\n✅ State committed to disk successfully.")

    # Pick the next randomized check time now that a real run has happened.
    next_interval = random.randint(MIN_INTERVAL_MIN, MAX_INTERVAL_MIN)
    next_run = datetime.now() + timedelta(minutes=next_interval)
    save_schedule({
        "last_run": datetime.now().isoformat(),
        "next_allowed_run": next_run.isoformat(),
        "interval_minutes": next_interval,
    })
    print(
        f"⏱️ Next actual BMS check randomized for ~{next_interval} minutes "
        f"(target time: {next_run.strftime('%Y-%m-%d %H:%M:%S')})."
    )

    for changes, movie_info, movie_url in pending_notifications:
        print(f"  ⚡ Sending {len(changes)} update(s) for {movie_info['name']}...")
        send_telegram_message(changes, movie_info, movie_url)

    print("\nBatch operations finished.")


if __name__ == "__main__":
    main()
