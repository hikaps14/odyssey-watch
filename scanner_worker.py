# ==============================================================================
# PROJECT ODYSSEY-WATCH: BACKGROUND DATA ENGINE (DUAL-MODE CLOUD WORKER)
# ==============================================================================
# File: scanner_worker.py
# Description: Automated scraper that fetches schedule pages and seat maps from
#              Cinemark, filters ADA seats, extracts back-row pair availability,
#              dispatches Discord webhooks, and commits atomic JSON cache snapshots.
# ==============================================================================

import os
import re
import html
import json
import time
import random
import sys
import argparse
import requests
from datetime import datetime, timedelta, time as dtime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

# ------------------------------------------------------------------------------
# 1. GLOBAL CONFIGURATION & ENVIRONMENT REGISTRY
# ------------------------------------------------------------------------------

# Discord Webhook Endpoint (Read from GitHub Secrets or fallback to sandbox string)
DISCORD_WEBHOOK_URL = os.getenv("https://discord.com/api/webhooks/1533962245720117358/GEKWnayfWaGgxNsCD-tHZdMe_mLSHzFDZ6Ex7MHO3vxB9dBs6Axq6_YQrHTdMGV4ZANn", "https://discord.com/api/webhooks/1533962245720117358/GEKWnayfWaGgxNsCD-tHZdMe_mLSHzFDZ6Ex7MHO3vxB9dBs6Axq6_YQrHTdMGV4ZANn")

# Target Facility & Film Anchor Identifiers
THEATER_ID = "207"                     # Fixed ID for Cinemark Dallas XD & IMAX
MOVIE_SLUG = "the-odyssey-imax-70mm"   # Film URL slug

# Target Schedule Blocks (Blocks 1, 2, and 3 cover Aug 07 -> Sep 16, 2026)
SELECTED_BLOCKS = [1, 2, 3]

# Geometric Seat Map Preferences
PREFERRED_ROWS = ['F', 'G', 'H', 'J', 'K', 'L']  # Back rows eligible for Discord alerts
ROW_RANKING_ORDER = ['L', 'K', 'J', 'H', 'G', 'F', 'E', 'D', 'C', 'B', 'A'] # Row priority

# Operational Delays (Throttling guards to maintain stealth and prevent 429 blocks)
MIN_DELAY_SEC = 2.0           # Seat map fetch delay floor (~3.0s average)
MAX_DELAY_SEC = 4.0           # Seat map fetch delay ceiling
SCHEDULE_MIN_DELAY = 1.5      # Schedule page discovery delay floor
SCHEDULE_MAX_DELAY = 2.5      # Schedule page discovery delay ceiling
COOLING_PERIOD_SEC = 30       # Backoff pause duration when HTTP 429 is encountered

# Local Storage Paths
CACHE_FILE_PATH = "cache_inventory.json"
STATE_FILE_PATH = "tracker_alert_state.json"
LOG_FILE_PATH = "odyssey_scan_history.txt"


# ------------------------------------------------------------------------------
# 2. PERSISTENCE & FILE I/O UTILITIES
# ------------------------------------------------------------------------------

def load_json_file(file_path: str) -> dict:
    """
    Safely reads a JSON file from disk with fallback to empty dict on failure.
    
    Args:
        file_path (str): Path to target JSON file.
    Returns:
        dict: Parsed JSON data or empty dict if non-existent/corrupted.
    """
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_atomic_json(file_path: str, data: dict) -> None:
    """
    Writes JSON data to a temporary file before replacing the target file.
    Prevents race conditions where Streamlit reads a partially written file.
    
    Args:
        file_path (str): Final target output path.
        data (dict): Python dictionary payload to persist.
    """
    temp_path = f"{file_path}.tmp"
    with open(temp_path, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(temp_path, file_path)


def append_scan_log(entry_text: str) -> None:
    """Appends timestamped operational audit logs to local text storage."""
    timestamp = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d %H:%M:%S CDT")
    with open(LOG_FILE_PATH, "a") as f:
        f.write(f"[{timestamp}] {entry_text}\n")


# ------------------------------------------------------------------------------
# 3. HTTP SESSION & HTML DOM PARSING ENGINES
# ------------------------------------------------------------------------------

def create_session() -> requests.Session:
    """
    Constructs an authenticated HTTP session pre-configured with Cinemark 
    location cookies and standard browser headers.
    """
    session = requests.Session()
    session.cookies.update({
        "theaterId": THEATER_ID,
        "preferredTheaterId": THEATER_ID,
        "CinemarkPreferredTheater": THEATER_ID,
        "userZipCode": "75234"
    })
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.cinemark.com/"
    })
    try:
        # Initial handshake to establish session context
        session.get("https://www.cinemark.com/theatres/tx-dallas/cinemark-dallas-xd-and-imax", timeout=10)
    except Exception:
        pass
    return session


def categorize_priority(raw_showtime_iso: str) -> tuple[int, str]:
    """
    Categorizes a showtime timestamp into priority tiers based on day & time.
    
    Returns:
        tuple[int, str]: (Priority Code 1-5, Priority Human Label)
    """
    if not raw_showtime_iso:
        return 4, "Priority 4 (Standard)"
    try:
        dt = datetime.strptime(raw_showtime_iso.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        st_time = dt.time()
        weekday = dt.weekday()  # 0=Mon, 3=Thu, 4=Fri, 5=Sat, 6=Sun
        
        # Priority 5: Hard-exclude 2:30 AM overnight showtimes
        if dtime(1, 30) <= st_time <= dtime(4, 0):
            return 5, "EXCLUDED (2:30 AM Overnight)"
            
        # Priority 1: Weekend Prime (Sat/Sun 11:00 AM - 8:00 PM)
        if weekday in (5, 6) and (dtime(11, 0) <= st_time <= dtime(20, 0)):
            return 1, "Priority 1 (Weekend Prime)"
            
        # Priority 2: Friday Evening (Fri 7:00 PM - 9:00 PM)
        if weekday == 4 and (dtime(19, 0) <= st_time <= dtime(21, 0)):
            return 2, "Priority 2 (Friday Evening)"
            
        # Priority 3: Mon/Thu Matinee (Mon/Thu 11:00 AM - 12:30 PM)
        if weekday in (0, 3) and (dtime(11, 0) <= st_time <= dtime(12, 30)):
            return 3, "Priority 3 (Mon/Thu Matinee)"
            
        # Priority 4: All other standard daily/evening showtimes
        return 4, "Priority 4 (Standard)"
    except Exception:
        return 4, "Priority 4 (Standard)"


def parse_html_seats(html_text: str) -> list[dict]:
    """
    Parses server-rendered HTML button tags from the seat map page, strictly
    identifying wheelchair, handicap, and companion attributes.
    """
    seats = []
    button_pattern = r'<button\s+[^>]*?info=["\'](?P<info>[^"\']+)["\'][^>]*?>'
    acc_keywords = ['wheelchair', 'companion', 'handicap', 'ada', 'accessible', 'seathandicap', 'seatcompanion']
    
    for match in re.finditer(button_pattern, html_text, re.IGNORECASE):
        tag_str = match.group(0)
        parts = [p.strip() for p in match.group('info').split(',')]
        if len(parts) >= 2:
            row = parts[0].upper()
            try:
                num = int(parts[1])
            except ValueError:
                continue
            
            is_avail = 'available="true"' in tag_str.lower() or 'seatavailable' in tag_str.lower()
            is_acc = any(kw in tag_str.lower() for kw in acc_keywords)
            
            seats.append({
                "row": row,
                "number": num,
                "status": "Available" if is_avail else "Occupied",
                "isAccessible": is_acc
            })
    return seats


def process_inventory(seats: list[dict]) -> dict:
    """
    Analyzes standard seat inventory, strictly excluding accessible/wheelchair
    seats from counts and row rankings.
    """
    std_open = [s for s in seats if s["status"] == "Available" and not s["isAccessible"]]
    if not std_open:
        return {"total_std_open": 0, "best_row": "Sold Out", "rows_breakdown": {}}
        
    rows_map = {}
    for s in std_open:
        rows_map.setdefault(s["row"], []).append(s["number"])
        
    best_row = "None"
    rows_breakdown = {}
    
    for r in ROW_RANKING_ORDER:
        if r in rows_map:
            nums = sorted(rows_map[r])
            pairs = sum(1 for i in range(len(nums)-1) if nums[i+1] - nums[i] == 1)
            is_back = r in PREFERRED_ROWS
            
            rows_breakdown[r] = {
                "open_count": len(nums),
                "pairs": pairs,
                "is_back_row": is_back,
                "seats": nums
            }
            if best_row == "None":
                row_tag = "PREFERRED BACK" if is_back else "FRONT/MID"
                best_row = f"Row {r} [{row_tag}] ({len(nums)} open, {pairs} pair(s))"
                
    return {
        "total_std_open": len(std_open),
        "best_row": best_row,
        "rows_breakdown": rows_breakdown
    }


def find_back_row_pairs(seats: list[dict]) -> list[dict]:
    """Evaluates back rows (F-L) for adjacent open standard pairs."""
    viable = [s for s in seats if s["status"] == "Available" and s["row"] in PREFERRED_ROWS and not s["isAccessible"]]
    viable.sort(key=lambda x: (x['row'], x['number']))
    pairs = []
    i = 0
    while i < len(viable) - 1:
        c, a = viable[i], viable[i+1]
        if c['row'] == a['row'] and abs(c['number'] - a['number']) == 1:
            pairs.append({
                "signature": f"{c['row']}-{c['number']}-{a['number']}",
                "label": f"Row {c['row']}: Seats {c['number']} & {a['number']}"
            })
            i += 2
        else:
            i += 1
    return pairs


def dispatch_discord(title: str, label: str, direct_url: str) -> bool:
    """Dispatches real-time notification embed to Discord webhook endpoint."""
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        return True
    payload = {
        "username": "Odyssey IMAX 70mm Monitor",
        "embeds": [{
            "title": title,
            "color": 15158332,
            "fields": [
                {"name": "🎬 Movie", "value": "The Odyssey - True IMAX 70mm", "inline": True},
                {"name": "💺 Location", "value": label, "inline": False},
                {"name": "🔗 Booking Link", "value": f"[Select Seats On Cinemark]({direct_url})", "inline": False}
            ]
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


# ------------------------------------------------------------------------------
# 4. MAIN ORCHESTRATION SCAN ENGINE
# ------------------------------------------------------------------------------

def run_scan_pass(allowed_priorities: list[int]) -> None:
    """
    Main execution loop that sweeps targeted schedule dates and seat maps.
    
    Args:
        allowed_priorities (list[int]): Tiers to process (e.g., [1,2,3] or [1,2,3,4]).
    """
    central_now = datetime.now(ZoneInfo("America/Chicago"))
    timestamp_iso = central_now.isoformat()
    timestamp_display = central_now.strftime("%a, %b %d, %Y at %I:%M %p CDT")
    
    print(f"🚀 [SCANNER WORKER] Starting pass at {timestamp_display} | Tiers: {allowed_priorities}", flush=True)
    
    session = create_session()
    alert_ledger = load_json_file(STATE_FILE_PATH)
    existing_cache = load_json_file(CACHE_FILE_PATH)
    state_mutated = False
    
    # Preserve prior cache data if running a partial sweep
    scanned_showtimes_map = {}
    if "showtimes" in existing_cache:
        for s in existing_cache["showtimes"]:
            scanned_showtimes_map[s["showtime_id"]] = s

    total_scanned = 0
    
    for block_num in SELECTED_BLOCKS:
        blocks_dates = {
            1: ("2026-08-07", "2026-08-20"),
            2: ("2026-08-21", "2026-09-03"),
            3: ("2026-09-04", "2026-09-16")
        }
        start_dt = datetime.strptime(blocks_dates[block_num][0], "%Y-%m-%d")
        end_dt = datetime.strptime(blocks_dates[block_num][1], "%Y-%m-%d")
        
        curr = start_dt
        while curr <= end_dt:
            date_str = curr.strftime("%Y-%m-%d")
            curr += timedelta(days=1)
            
            # Throttling delay before fetching schedule page
            time.sleep(round(random.uniform(SCHEDULE_MIN_DELAY, SCHEDULE_MAX_DELAY), 2))
            
            sched_url = f"https://www.cinemark.com/movies/{MOVIE_SLUG}?showDate={date_str}&theaterId={THEATER_ID}"
            try:
                res = session.get(sched_url, timeout=12)
                if res.status_code == 429:
                    print("   ⚠️ Rate limit hit on schedule. Cooling down 30s...", flush=True)
                    time.sleep(COOLING_PERIOD_SEC)
                    continue
                clean_html = html.unescape(res.text)
            except Exception:
                continue
                
            matches = re.findall(r'showTimeUrl["\']?\s*:\s*["\']\?([^"\']+)["\']', clean_html, re.IGNORECASE)
            seen = set()
            
            for rel_qs in matches:
                qs = parse_qs(rel_qs)
                s_id = qs.get("ShowtimeId", [None])[0]
                t_id = qs.get("TheaterId", [THEATER_ID])[0]
                raw_st = qs.get("Showtime", [""])[0]
                
                if t_id == THEATER_ID and s_id and s_id not in seen:
                    seen.add(s_id)
                    p_code, p_label = categorize_priority(raw_st)
                    
                    # Process matching priority tiers (Priority 5 always excluded)
                    if p_code in allowed_priorities and p_code != 5:
                        total_scanned += 1
                        
                        # Throttling delay before fetching seat map page
                        time.sleep(round(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC), 2))
                        
                        seat_url = f"https://www.cinemark.com/TicketSeatMap/?{rel_qs}"
                        try:
                            seat_res = session.get(seat_url, timeout=12)
                            if seat_res.status_code == 429:
                                print(f"   ⚠️ Rate limit hit on Showtime {s_id}. Cooling down 30s...", flush=True)
                                time.sleep(COOLING_PERIOD_SEC)
                                continue
                            raw_seats = parse_html_seats(html.unescape(seat_res.text))
                        except Exception:
                            continue
                            
                        inv = process_inventory(raw_seats)
                        back_pairs = find_back_row_pairs(raw_seats)
                        
                        dt_obj = datetime.strptime(raw_st.split(".")[0], "%Y-%m-%dT%H:%M:%S") if raw_st else None
                        
                        showtime_record = {
                            "block": block_num,
                            "showtime_id": s_id,
                            "date_iso": date_str,
                            "date_display": dt_obj.strftime("%a, %b %d") if dt_obj else date_str,
                            "time_display": dt_obj.strftime("%I:%M %p") if dt_obj else "Unknown",
                            "day_name": dt_obj.strftime("%A") if dt_obj else "",
                            "is_weekend": dt_obj.weekday() in (4, 5, 6) if dt_obj else False,
                            "priority_tier": p_code,
                            "priority_label": p_label,
                            "total_std_open": inv["total_std_open"],
                            "best_row_desc": inv["best_row"],
                            "rows_breakdown": inv["rows_breakdown"],
                            "prime_back_row_pairs": back_pairs,
                            "has_back_row_pair": len(back_pairs) > 0,
                            "direct_booking_url": seat_url
                        }
                        
                        # Upsert record into active dataset
                        scanned_showtimes_map[s_id] = showtime_record
                        
                        # Noise Gate: Alert only once per unique back-row pair signature
                        for pair in back_pairs:
                            sig_key = f"{s_id}_{pair['signature']}"
                            if sig_key not in alert_ledger:
                                if dispatch_discord(f"💺 Prime Back-Row Pair: {showtime_record['date_display']} at {showtime_record['time_display']}", pair["label"], seat_url):
                                    alert_ledger[sig_key] = timestamp_iso
                                    state_mutated = True

    # Build final cache payload
    cache_data = {
        "last_updated_iso": timestamp_iso,
        "last_updated_display": timestamp_display,
        "blocks_scanned": SELECTED_BLOCKS,
        "allowed_priorities": allowed_priorities,
        "total_scanned_count": total_scanned,
        "showtimes": list(scanned_showtimes_map.values())
    }

    # Atomically persist updated cache and alert ledger
    save_atomic_json(CACHE_FILE_PATH, cache_data)
    if state_mutated:
        save_atomic_json(STATE_FILE_PATH, alert_ledger)
        
    print(f"✅ [SCANNER WORKER] Pass complete ({total_scanned} shows scanned). Saved to '{CACHE_FILE_PATH}'.", flush=True)


# ------------------------------------------------------------------------------
# 5. CLI ENTRY POINT & DUAL-MODE DISPATCH
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Odyssey IMAX 70mm Scraper Worker")
    parser.add_argument(
        "--mode", 
        choices=["prime", "full"], 
        default="prime", 
        help="Scan mode: 'prime' (Tiers 1-3) or 'full' (Tiers 1-4)"
    )
    args = parser.parse_args()
    
    # Dual Mode Dispatch Strategy
    if args.mode == "full":
        target_priorities = [1, 2, 3, 4]  # Full Sweep (Everything except 2:30 AM overnight)
    else:
        target_priorities = [1, 2, 3]     # Prime Pass (Weekend Prime, Fri Eve, Matinees)
        
    run_scan_pass(target_priorities)
