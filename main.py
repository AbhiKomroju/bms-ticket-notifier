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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Multi-Ticket Checker Active (debug={'ON' if DEBUG else 'off'})")

    urls = [u.strip() for u in CONFIG["url"].split(",") if u.strip()]
    if not urls:
        print("  ❌ No URLs found in BMS_URL environment variable.")
        sys.exit(1)

    # ── Randomized scheduling gate ──────────────────────────────────────
    # The workflow's cron ticks on a tight fixed schedule (e.g. every 10-15
    # min), but we only do real work on a randomly-chosen subset of those
    # ticks, spaced MIN_INTERVAL-MAX_INTERVAL minutes apart. Skipped ticks
    # exit here before any network call — cheap and doesn't touch state.
    schedule = load_schedule()
    next_allowed_str = schedule.get("next_allowed_run")
    now = datetime.now()
    if next_allowed_str and not FORCE_RUN:
        try:
            next_allowed = datetime.fromisoformat(next_allowed_str)
        except ValueError:
            next_allowed = now
        if now < next_allowed:
            remaining = (next_allowed - now).total_seconds() / 60
            print(f"  ⏭️  Not due yet — next check in ~{remaining:.1f} min "
                  f"(scheduled for {next_allowed.strftime('%H:%M:%S')}). "
                  f"Randomized interval: {MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN} min.")
            sys.exit(0)
    elif FORCE_RUN:
        print("  ⚡ BMS_FORCE_RUN=1 — bypassing the randomized schedule gate.")

    old_state = load_state()
    new_state = {"shows": {}, "dates": {}}
    if old_state:
        new_state["shows"].update(old_state.get("shows", {}))
        new_state["dates"].update(old_state.get("dates", {}))

    pending_notifications = []

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
                movie_info = parse_movie_info(data, movie_url)
            movie_dates.extend(parse_dates(data))
            movie_shows.extend(parse_shows(data))

        if not movie_shows:
            print(f"  ❌ No current showtimes found for this target configuration.")
            continue

        print(f"  🎬 {movie_info['name']} ({movie_info['language']})")

        # Surface every distinct screen/audi name seen for this movie so you
        # can copy the exact string into BMS_SCREEN without guessing.
        seen_screens = sorted({s.screen_attr for s in movie_shows if s.screen_attr})
        if seen_screens:
            print(f"  🖥️  Screens seen: {', '.join(seen_screens)}")
        elif DEBUG:
            dbg("No screen_attr values populated on any show — check RAW API "
                "RESPONSE above for where the screen/audi name actually lives.")

        filtered = filter_shows(
            movie_shows,
            CONFIG["theatre"],
            CONFIG["screen"],
            CONFIG["time_period"],
            CONFIG["dates"],
        )
        print(f"  📊 {len(filtered)} showtime(s) matching criteria filters")

        append_to_state(new_state, event_code, filtered, movie_dates)

        if old_state:
            changes = detect_movie_changes(old_state, new_state, event_code)
            if changes:
                pending_notifications.append((changes, movie_info, movie_url))
            else:
                print("  ✅ No structural updates caught since the last run cycle.")

    save_state(new_state)
    print("\nState Committed to disk successfully.")

    # Pick the next randomized check time now that a real run has happened.
    next_interval = random.randint(MIN_INTERVAL_MIN, MAX_INTERVAL_MIN)
    next_run = datetime.now() + timedelta(minutes=next_interval)
    save_schedule({
        "last_run": datetime.now().isoformat(),
        "next_allowed_run": next_run.isoformat(),
        "interval_minutes": next_interval,
    })
    print(f"⏱️  Next check randomized for ~{next_interval} min from now "
          f"(~{next_run.strftime('%H:%M:%S')}).")

    for changes, movie_info, movie_url in pending_notifications:
        print(f"  ⚡ {len(changes)} update(s) caught for {movie_info['name']}!")
        send_telegram_message(changes, movie_info, movie_url)

    print("\nBatch Operations Finished.")


if __name__ == "__main__":
    main()