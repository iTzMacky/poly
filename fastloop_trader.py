#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill – Closest active market selector (fixed)
"""

import os
import sys
import json
import argparse
import time
import re
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from dateutil import parser
from dateutil.tz import gettz

sys.stdout.reconfigure(line_buffering=True)

# ────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────

ENTRY_THRESHOLD     = float(os.environ.get("SIMMER_SPRINT_ENTRY",     0.05))
MIN_MOMENTUM_PCT    = float(os.environ.get("SIMMER_SPRINT_MOMENTUM",  0.5))
MAX_POSITION_USD    = float(os.environ.get("SIMMER_SPRINT_MAX_POSITION", 5.0))
SIGNAL_SOURCE       = os.environ.get("SIMMER_SPRINT_SIGNAL", "coingecko")
LOOKBACK_MINUTES    = int(os.environ.get("SIMMER_SPRINT_LOOKBACK",    5))
MIN_TIME_REMAINING  = int(os.environ.get("SIMMER_SPRINT_MIN_TIME",    60))
ASSET               = os.environ.get("SIMMER_SPRINT_ASSET", "BTC").upper()
WINDOW              = os.environ.get("SIMMER_SPRINT_WINDOW", "5m")
VOLUME_CONFIDENCE   = os.environ.get("SIMMER_SPRINT_VOL_CONF", "true").lower() == "true"

SIMMER_API_KEY = os.environ.get("SIMMER_API_KEY")
if not SIMMER_API_KEY:
    print("Error: SIMMER_API_KEY environment variable not set")
    sys.exit(1)

SIMMER_BASE = os.environ.get("SIMMER_API_BASE", "https://api.simmer.markets")

# ────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────

def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    try:
        headers = headers or {"User-Agent": "fastloop-trader/1.0"}
        if data:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = None
        req = Request(url, data=body, headers=headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"API request failed: {str(e)}")
        return {"error": str(e)}

def simmer_request(path, method="GET", data=None):
    headers = {"Authorization": f"Bearer {SIMMER_API_KEY}"}
    return _api_request(f"{SIMMER_BASE}{path}", method=method, data=data, headers=headers)

# ────────────────────────────────────────────────
# MARKET DISCOVERY & PARSING – IMPROVED
# ────────────────────────────────────────────────

def discover_fast_market_markets():
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=100&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api_request(url)
    if "error" in result or not isinstance(result, list):
        print("Gamma API error:", result.get("error", result))
        return []

    markets = []
    for m in result:
        question = m.get("question", "")
        q_lower = question.lower()
        slug = m.get("slug", "")
        if "bitcoin up or down" in q_lower and f"-{WINDOW}-" in slug and not m.get("closed", False):
            end_time = parse_end_time(question, slug)
            if end_time:
                markets.append({
                    "question": question,
                    "slug": slug,
                    "condition_id": m.get("conditionId"),
                    "end_time": end_time,
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                    "end_date_iso": m.get("endDateIso") or m.get("endDate", ""),
                })

    return markets

def parse_end_time(question, slug):
    # Improved regex for titles like "Bitcoin Up or Down - February 16, 9:05AM-9:10AM ET"
    pattern = r'(\w+ \d+)[,;]?\s*(\d{1,2}(?::\d{2})?[AP]M?)\s*[-–]\s*\d{1,2}(?::\d{2})?[AP]M?\s*ET'
    match = re.search(pattern, question, re.IGNORECASE)
    if match:
        date_part = match.group(1)
        time_part = match.group(2)
        try:
            dt_str = f"{date_part} {datetime.now().year} {time_part} ET"
            dt = parser.parse(dt_str, fuzzy=True, tzinfos={"ET": gettz("America/New_York")})
            return dt.astimezone(timezone.utc)
        except Exception as e:
            print(f"Question parse failed: {e}")

    # Strong fallback: slug timestamp is usually start time → add 5 min
    slug_match = re.search(r'-(\d{10,})$', slug)
    if slug_match:
        try:
            unix_ts = int(slug_match.group(1))
            start_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
            end_dt = start_dt + timedelta(minutes=5)
            # print(f"Slug fallback: {slug} → end {end_dt.isoformat()}")
            return end_dt
        except Exception as e:
            print(f"Slug parse failed: {e}")

    print(f"Could not parse end time for: {question} | {slug}")
    return None

def select_soonest_market(markets):
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if end_time:
            remaining_sec = (end_time - now).total_seconds()
            if remaining_sec > MIN_TIME_REMAINING:
                candidates.append((remaining_sec, m))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])  # smallest remaining first
    return candidates[0][1]

# ────────────────────────────────────────────────
# MAIN CYCLE
# ────────────────────────────────────────────────

def run_cycle(dry_run=True):
    print("⚡ Simmer FastLoop Trading Skill")
    print("=" * 50)
    if dry_run:
        print("  [DRY RUN] No trades will be executed.")

    print(f"\nConfiguration:")
    print(f"  Asset:            {ASSET}")
    print(f"  Window:           {WINDOW}")
    print(f"  Signal:           {SIGNAL_SOURCE}")
    print(f"  Min momentum:     {MIN_MOMENTUM_PCT}%")
    print(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    print(f"  Min time left:    {MIN_TIME_REMAINING}s")

    markets = discover_fast_market_markets()
    print(f"\nFound {len(markets)} active fast markets")

    if markets:
        print("\nDiscovered markets (showing first 5 – sorted by creation):")
        now = datetime.now(timezone.utc)
        for m in markets[:5]:
            remaining_sec = "N/A"
            if m["end_time"]:
                remaining_sec = (m["end_time"] - now).total_seconds()
                remaining_str = f"{remaining_sec:,.0f}s  (~{remaining_sec/3600:.1f} hours)"
            else:
                remaining_str = "parse failed"
            print(f" - {m['question']}")
            print(f"   Expires ~{remaining_str} | Slug: {m['slug']} | End ISO: {m.get('end_date_iso','N/A')}")

    if not markets:
        print("No suitable markets found.")
        return

    best = select_soonest_market(markets)
    if not best:
        print(f"No markets with >{MIN_TIME_REMAINING}s remaining")
        return

    remaining = (best["end_time"] - datetime.now(timezone.utc)).total_seconds()
    hours = remaining / 3600
    print(f"\n🎯 Selected closest active market:")
    print(f"   {best['question']}")
    print(f"   Expires in: {remaining:,.0f}s  (~{hours:.1f} hours)")
    print(f"   Slug: {best['slug']}")

    if remaining > 43200:  # > 12 hours
        print("⚠️  This is a pre-created market for tomorrow.")
        print("    Liquidity is usually very low until closer to the start time.")

    # Placeholder for next steps
    print("\nNext steps would be:")
    print("  1. Import market to Simmer")
    print("  2. Fetch price momentum")
    print("  3. Decide YES/NO trade")

    print("\n📊 Cycle complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Execute real trades")
    args = parser.parse_args()

    run_cycle(dry_run=not args.live)
