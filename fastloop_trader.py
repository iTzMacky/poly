#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
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
from urllib.parse import urlencode
from dateutil import parser
from dateutil.tz import gettz

sys.stdout.reconfigure(line_buffering=True)

# Trade Journal stub
JOURNAL_AVAILABLE = False
def log_trade(*args, **kwargs): pass

# Config (hard-coded defaults; expand if needed)
ENTRY_THRESHOLD = 0.05
MIN_MOMENTUM_PCT = 0.5
MAX_POSITION_USD = 5.0
SIGNAL_SOURCE = "binance"  # or "coingecko"
LOOKBACK_MINUTES = 5
MIN_TIME_REMAINING = 60
ASSET = "BTC"
WINDOW = "5m"
VOLUME_CONFIDENCE = True

ASSET_PATTERNS = {"BTC": ["bitcoin up or down"]}

TRADE_SOURCE = "sdk:fastloop"

SIMMER_BASE = os.environ.get("SIMMER_API_BASE", "https://api.simmer.markets")

def get_api_key():
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        print("Error: SIMMER_API_KEY not set")
        sys.exit(1)
    return key

def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    try:
        req_headers = headers or {"User-Agent": "simmer-fastloop/1.0"}
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        else:
            body = None
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}

def simmer_request(path, method="GET", data=None, api_key=None):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return _api_request(f"{SIMMER_BASE}{path}", method=method, data=data, headers=headers)

# =============================================================================
# Market Discovery & Parsing
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m"):
    patterns = ASSET_PATTERNS.get(asset, ["bitcoin up or down"])
    url = "https://gamma-api.polymarket.com/markets?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    result = _api_request(url)
    if not result or "error" in result:
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        if any(p in q for p in patterns) and f"-{window}-" in slug and not m.get("closed", False) and slug:
            question = m.get("question", "")
            end_time = _parse_fast_market_end_time(question, slug)
            markets.append({
                "question": question,
                "slug": slug,
                "condition_id": m.get("conditionId", ""),
                "end_time": end_time,
                "outcome_prices": m.get("outcomePrices", "[]"),
                "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                "end_date_iso": m.get("endDateIso") or m.get("endDate", ""),
            })
    return markets

def _parse_fast_market_end_time(question, slug):
    # Primary: parse from question title
    # Pattern for "Bitcoin Up or Down - February 16, 9:05AM-9:10AM ET"
    pattern = r'(\w+ \d+)[,;]?\s*(\d{1,2}(?::\d{2})?[AP]M?)\s*-\s*\d{1,2}(?::\d{2})?[AP]M?\s*ET'
    match = re.search(pattern, question, re.IGNORECASE)
    if match:
        date_part = match.group(1)
        time_part = match.group(2)
        try:
            dt_str = f"{date_part} {datetime.now().year} {time_part} ET"
            dt = parser.parse(dt_str, fuzzy=True, tzinfos={"ET": gettz("America/New_York")})
            return dt.astimezone(timezone.utc)
        except:
            pass

    # Fallback: extract Unix timestamp from slug e.g. btc-updown-5m-1771250700 → start time
    slug_match = re.search(r'-(\d{10,})$', slug)
    if slug_match:
        try:
            unix_ts = int(slug_match.group(1))
            start_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
            # Assume 5m market: end = start + 5 min
            return start_dt + timedelta(minutes=5)
        except:
            pass

    return None

def find_best_fast_market(markets):
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if end_time:
            remaining = (end_time - now).total_seconds()
            if remaining > MIN_TIME_REMAINING:
                candidates.append((remaining, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

# =============================================================================
# Price Fetch (with retries)
# =============================================================================

def get_coingecko_momentum(asset="bitcoin"):
    for attempt in range(3):
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset}&vs_currencies=usd"
        result = _api_request(url)
        if result and asset in result and "usd" in result[asset]:
            price = result[asset]["usd"]
            return {"momentum_pct": 0, "direction": "neutral", "price_now": price, "price_then": price}
        time.sleep(5)
    return None

def get_momentum(asset="BTC", source="coingecko"):
    if source == "coingecko":
        return get_coingecko_momentum("bitcoin")
    # Add Binance if needed
    return None

# =============================================================================
# Main
# =============================================================================

def run_fast_market_strategy(dry_run=True, quiet=False):
    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    log("⚡ Simmer FastLoop Trading Skill")
    log("=" * 50)
    if dry_run:
        log("  [DRY RUN] No trades will be executed.")

    log(f"\nConfiguration: Asset={ASSET}, Window={WINDOW}, Signal={SIGNAL_SOURCE}, Min momentum={MIN_MOMENTUM_PCT}%")

    api_key = get_api_key()

    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets = discover_fast_market_markets(ASSET, WINDOW)
    log(f"  Found {len(markets)} active fast markets")

    if markets:
        log("Discovered markets (first 5):")
        for m in markets[:5]:
            remaining = "N/A"
            if m.get('end_time'):
                remaining = f"{(m['end_time'] - datetime.now(timezone.utc)).total_seconds():.0f}s"
            log(f" - {m['question']} | Expires ~{remaining} | Slug: {m['slug']} | End ISO: {m.get('end_date_iso','N/A')}")

    if not markets:
        log("No markets found.")
        return

    best = find_best_fast_market(markets)
    if not best:
        log(f"No fast markets with >{MIN_TIME_REMAINING}s remaining")
        return

    remaining = (best['end_time'] - datetime.now(timezone.utc)).total_seconds() if best.get('end_time') else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    # Continue with your price fetch, analysis, trade logic...
    log("Price fetch & analysis would go here...")

    print("\n📊 Cycle complete")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    run_fast_market_strategy(dry_run=not args.live, quiet=args.quiet)
