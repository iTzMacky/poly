#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Agents can customize signal source.
"""

import os
import sys
import json
import math
import argparse
import time
import re
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from dateutil import parser
from dateutil.tz import gettz

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass

# =============================================================================
# Configuration
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_SPRINT_ENTRY", "type": float},
    "min_momentum_pct": {"default": 0.5, "env": "SIMMER_SPRINT_MOMENTUM", "type": float},
    "max_position": {"default": 5.0, "env": "SIMMER_SPRINT_MAX_POSITION", "type": float},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str},
    "lookback_minutes": {"default": 5, "env": "SIMMER_SPRINT_LOOKBACK", "type": int},
    "min_time_remaining": {"default": 60, "env": "SIMMER_SPRINT_MIN_TIME", "type": int},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str},
    "volume_confidence": {"default": True, "env": "SIMMER_SPRINT_VOL_CONF", "type": bool},
}

TRADE_SOURCE = "sdk:fastloop"
SMART_SIZING_PCT = 0.05
MIN_SHARES_PER_ORDER = 5

ASSET_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}

cfg = {}  # will be filled below
for key, spec in CONFIG_SCHEMA.items():
    cfg[key] = spec["default"]

# Load config (simplified for clarity - you can keep the full loader if needed)
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
MIN_TIME_REMAINING = cfg["min_time_remaining"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]
VOLUME_CONFIDENCE = cfg["volume_confidence"]

SIMMER_BASE = os.environ.get("SIMMER_API_BASE", "https://api.simmer.markets")

# =============================================================================
# API Helpers
# =============================================================================

def get_api_key():
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        print("Error: SIMMER_API_KEY not set")
        print("Get it from: simmer.markets/dashboard → SDK tab")
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
# Market Discovery
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m"):
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = "https://gamma-api.polymarket.com/markets?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    result = _api_request(url)
    if not result or "error" in result:
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        if any(p in q for p in patterns) and f"-{window}-" in slug:
            if not m.get("closed", False) and slug:
                end_time = _parse_fast_market_end_time(m.get("question", ""))
                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": m.get("conditionId", ""),
                    "end_time": end_time,
                    "outcomes": m.get("outcomes", []),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                    "end_date_iso": m.get("endDateIso") or m.get("endDate", ""),
                })

    # Prefer today's markets, fallback to tomorrow
    now = datetime.now(timezone.utc)
    today_iso = now.strftime("%Y-%m-%d")
    tomorrow_iso = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    today_markets = [m for m in markets if today_iso in m.get("end_date_iso", "")]
    if today_markets:
        return today_markets

    tomorrow_markets = [m for m in markets if tomorrow_iso in m.get("end_date_iso", "")]
    return tomorrow_markets if tomorrow_markets else markets  # fallback to all


def _parse_fast_market_end_time(question):
    patterns = [
        r'(\w+ \d+)[,;]?\s*(?:.*?-)?\s*(\d{1,2}:\d{2}(?:AM|PM)?)\s*(?:-\s*\d{1,2}:\d{2}(?:AM|PM)?)?\s*ET',
        r'(\w+ \d+)[,;]?\s*(\d{1,2}(?::\d{2})?(?:AM|PM)?)\s*ET',
    ]
    for pat in patterns:
        match = re.search(pat, question, re.IGNORECASE)
        if match:
            date_part = match.group(1) or datetime.now().strftime("%B %d")
            time_part = match.group(2)
            try:
                dt_str = f"{date_part} {datetime.now().year} {time_part}"
                dt = parser.parse(dt_str, fuzzy=True)
                et_tz = gettz('America/New_York')
                dt = dt.replace(tzinfo=et_tz)
                return dt.astimezone(timezone.utc)
            except:
                continue
    return None


def find_best_fast_market(markets):
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining > MIN_TIME_REMAINING:
            candidates.append((remaining, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]

# =============================================================================
# Price Signal (unchanged except retries already added)
# =============================================================================

def get_coingecko_momentum(asset="bitcoin", lookback_minutes=5):
    for attempt in range(3):
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset}&vs_currencies=usd"
        result = _api_request(url)
        if result and isinstance(result, dict) and asset in result and "usd" in result[asset]:
            price_now = result[asset]["usd"]
            return {
                "momentum_pct": 0,
                "direction": "neutral",
                "price_now": price_now,
                "price_then": price_now,
                "avg_volume": 0,
                "latest_volume": 0,
                "volume_ratio": 1.0,
                "candles": 0,
            }
        time.sleep(5)
    return None

# ... (rest of get_binance_momentum, get_momentum unchanged)

# =============================================================================
# Main Logic (cleaned up logging)
# =============================================================================

def run_fast_market_strategy(dry_run=True, positions_only=False, show_config=False,
                             smart_sizing=False, quiet=False):
    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    log("⚡ Simmer FastLoop Trading Skill")
    log("=" * 50)

    if dry_run:
        log("  [DRY RUN] No trades will be executed. Use --live to enable trading.")

    log(f"\n⚙️  Configuration:")
    log(f"  Asset:            {ASSET}")
    log(f"  Window:           {WINDOW}")
    log(f"  Entry threshold:  {ENTRY_THRESHOLD}")
    log(f"  Min momentum:     {MIN_MOMENTUM_PCT}%")
    log(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    log(f"  Signal source:    {SIGNAL_SOURCE}")
    log(f"  Lookback:         {LOOKBACK_MINUTES} minutes")
    log(f"  Min time left:    {MIN_TIME_REMAINING}s")

    if show_config:
        return

    api_key = get_api_key()

    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets = discover_fast_market_markets(ASSET, WINDOW)
    log(f"  Found {len(markets)} active fast markets")

    # Debug print discovered markets
    if markets:
        log("Discovered markets (first 5):")
        for m in markets[:5]:
            remaining = (m['end_time'] - datetime.now(timezone.utc)).total_seconds() if m.get('end_time') else "N/A"
            log(f" - {m['question']} | Expires ~{remaining}s | Slug: {m['slug']} | End ISO: {m.get('end_date_iso','N/A')}")
    else:
        log("No markets found. Possible causes: no active 5m markets, filter too strict, API issue.")

    if not markets:
        return

    best = find_best_fast_market(markets)
    if not best:
        log(f"No fast markets with >{MIN_TIME_REMAINING}s remaining")
        return

    remaining = (best['end_time'] - datetime.now(timezone.utc)).total_seconds() if best.get('end_time') else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    # ... (rest of the function remains the same: price fetch, analysis, trade logic)
    # For brevity, I didn't repeat the entire 200+ lines of trading logic here.
    # Keep your original trading code from "Parse current market odds" onward.

    # Summary at end
    print("\n📊 Summary: Cycle complete")

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--positions", action="store_true")
    parser.add_argument("--config", action="store_true")
    parser.add_argument("--smart-sizing", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    dry_run = not args.live

    run_fast_market_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        quiet=args.quiet,
    )
