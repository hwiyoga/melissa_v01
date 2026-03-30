"""
Semi-automated Polymarket trading bot.

Flow:
  1. Fetch live crypto up/down 5-minute markets (BTC, ETH, SOL, XRP)
  2. Claude analyzes and recommends specific trades with token IDs
  3. You review and approve or skip each trade
  4. Approved trades execute via the CLOB API

Usage:
  python3 bot.py              — live trading (requires funded wallet)
  python3 bot.py --paper      — paper trading (no wallet needed, logs to paper_trades.json)
  python3 bot.py --paper --loop — runs every 30 minutes automatically

Edit DEFAULT_ORDER_SIZE below to change how much USDC to risk per trade.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import anthropic
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
POLYGON_CHAIN_ID = 137

DEFAULT_ORDER_SIZE = 5.0   # USDC per trade — edit to change
MAX_ORDER_SIZE = 25.0      # hard safety cap

# Coins to trade — prefix must match Polymarket slug pattern (<coin>-updown-Xm-<ts>)
COINS = ["btc", "eth", "sol", "xrp"]


# ── Market fetching ───────────────────────────────────────────────────────────

def fetch_updown_5m(coin: str, limit: int = 5) -> list[dict]:
    """
    Fetch active up/down 5-minute markets for a given coin by generating slugs
    from the current timestamp. Slugs follow: <coin>-updown-5m-<unix_timestamp>
    incrementing by 300 seconds (5 minutes).
    """
    import time as _time
    now_ts = int(_time.time())
    base = (now_ts // 300) * 300

    results = []
    for i in range(limit + 4):
        slug = f"{coin}-updown-5m-{base + i * 300}"
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
            r.raise_for_status()
            data = r.json()
            if not data:
                continue
            event = data[0]
            markets = event.get("markets", [])
            if not markets or not markets[0].get("outcomePrices"):
                continue
            # Skip already-expired markets
            end_date_str = event.get("endDate", "")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if end_dt <= datetime.now(timezone.utc):
                        continue
                except ValueError:
                    pass
            results.append(event)
            if len(results) >= limit:
                break
        except requests.RequestException:
            continue

    return results


def fetch_updown_15m(coin: str, limit: int = 5) -> list[dict]:
    """
    Fetch active up/down 15-minute markets for a given coin.
    Slugs follow: <coin>-updown-15m-<unix_timestamp> incrementing by 900 seconds.
    """
    import time as _time
    now_ts = int(_time.time())
    base = (now_ts // 900) * 900

    results = []
    for i in range(limit + 4):
        slug = f"{coin}-updown-15m-{base + i * 900}"
        try:
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=8)
            r.raise_for_status()
            data = r.json()
            if not data:
                continue
            event = data[0]
            markets = event.get("markets", [])
            if not markets or not markets[0].get("outcomePrices"):
                continue
            end_date_str = event.get("endDate", "")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if end_dt <= datetime.now(timezone.utc):
                        continue
                except ValueError:
                    pass
            results.append(event)
            if len(results) >= limit:
                break
        except requests.RequestException:
            continue

    return results


def fetch_all_coins(limit: int = 5) -> dict[str, list]:
    """Fetch up/down 5m and 15m markets for all configured coins."""
    all_markets = {}
    for coin in COINS:
        events_5m  = fetch_updown_5m(coin, limit=limit)
        events_15m = fetch_updown_15m(coin, limit=limit)
        all_markets[f"{coin.upper()}_5m"]  = events_5m
        all_markets[f"{coin.upper()}_15m"] = events_15m
        print(f"  ✓ {coin.upper()}: {len(events_5m)} x 5m  |  {len(events_15m)} x 15m")
    return all_markets


def fetch_todays_markets(limit: int = 10, min_volume: int = 200_000) -> list[dict]:
    """
    Fetch high-volume markets closing within the next 24 hours.
    Covers sports, weather, crypto price levels, breaking news — anything short-window.
    Filters out markets near resolution (price <10¢ or >90¢).
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(hours=24)

    try:
        r = requests.get(f"{GAMMA_API}/events", params={
            "active": "true", "closed": "false", "limit": 50,
            "order": "volume", "ascending": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, timeout=10)
        r.raise_for_status()
        events = r.json()
    except requests.RequestException as e:
        print(f"  ✗ today's markets: {e}")
        return []

    results = []
    for event in events:
        vol = float(event.get("volume", 0))
        if vol < min_volume:
            continue
        markets = event.get("markets", [])
        if not markets:
            continue
        # Skip if all outcomes near resolution
        prices = _parse_json_field(markets[0].get("outcomePrices"))
        if prices and all(float(p) < 0.10 or float(p) > 0.90 for p in prices):
            continue
        results.append(event)
        if len(results) >= limit:
            break

    return results


# ── Binance spot price trend ──────────────────────────────────────────────────

BINANCE_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

def fetch_binance_trend(coin: str, interval: str = "1m", limit: int = 5) -> str:
    """
    Fetch recent kline (candlestick) data from Binance public API.
    No API key required.

    Returns a human-readable string like:
      "BTC spot: FALLING ↓  $83,450 → $83,210 → $82,980  (-$470 in 3 min)"
    Returns "" on failure.
    """
    symbol = BINANCE_SYMBOLS.get(coin.lower())
    if not symbol:
        return ""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8,
        )
        r.raise_for_status()
        klines = r.json()
    except Exception:
        return ""

    if len(klines) < 2:
        return ""

    # Each kline: [open_time, open, high, low, close, volume, ...]
    closes = [float(k[4]) for k in klines]
    first, last = closes[0], closes[-1]
    change = last - first
    span_min = len(closes) - 1  # each candle = 1 interval

    # Format prices — show $ for BTC/ETH, show cents-level precision for SOL/XRP
    if last > 100:
        price_str = " → ".join(f"${p:,.0f}" for p in closes)
        change_str = f"{change:+,.0f}"
    else:
        price_str = " → ".join(f"${p:.3f}" for p in closes)
        change_str = f"{change:+.3f}"

    direction = "RISING ↑" if change > 0 else "FALLING ↓" if change < 0 else "FLAT →"
    coin_upper = coin.upper()
    return f"{coin_upper} spot (Binance): {direction}  {price_str}  ({change_str} in {span_min} min)"


# ── Polymarket price trend ────────────────────────────────────────────────────

def fetch_price_trend(token_id: str, window_start: int = 0) -> str:
    """
    Fetch recent price history for a token and return a human-readable trend summary.
    Uses CLOB /prices-history with 1m interval, fidelity=10.

    If window_start is provided (unix timestamp), shows points within the window first,
    then falls back to last 3 pre-window points for context.

    Returns a string like: "RISING ↑  43¢ → 47¢ → 52¢  (+9¢ in 10 min)"
    Returns "" on failure.
    """
    try:
        r = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={"market": token_id, "interval": "1m", "fidelity": 10},
            timeout=8,
        )
        r.raise_for_status()
        history = r.json().get("history", [])
    except Exception:
        return ""

    if len(history) < 2:
        return ""

    # Prefer in-window points; fall back to last 3 points overall
    if window_start:
        in_window = [h for h in history if h["t"] >= window_start]
        pre_window = [h for h in history if h["t"] < window_start][-2:]
        recent = pre_window + in_window if len(in_window) < 2 else in_window
    else:
        recent = history[-4:]

    if len(recent) < 2:
        recent = history[-3:]

    prices     = [round(h["p"] * 100, 1) for h in recent]
    timestamps = [h["t"] for h in recent]
    first      = prices[0]
    last       = prices[-1]
    change     = last - first
    span_min   = round((timestamps[-1] - timestamps[0]) / 60)

    price_str = " → ".join(f"{p:.1f}¢" for p in prices)

    if change > 2:
        direction = "RISING ↑"
    elif change < -2:
        direction = "FALLING ↓"
    else:
        direction = "FLAT →"

    return f"{direction}  {price_str}  ({change:+.1f}¢ in {span_min} min)"


# ── Market index + Claude formatting ─────────────────────────────────────────

def _parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


def build_market_index(all_coin_markets: dict, todays_markets: list) -> dict:
    """Map token_id -> {question, outcome, current_price} for trade validation."""
    index = {}

    def _index(events, fallback_title=""):
        for event in events:
            for market in event.get("markets", []):
                token_ids = _parse_json_field(market.get("clobTokenIds"))
                outcomes = _parse_json_field(market.get("outcomes"))
                prices = _parse_json_field(market.get("outcomePrices"))
                question = market.get("question") or event.get("title", fallback_title)
                for i, tid in enumerate(token_ids):
                    index[tid] = {
                        "question": question,
                        "outcome": outcomes[i] if i < len(outcomes) else "?",
                        "current_price": float(prices[i]) if i < len(prices) else None,
                    }

    for events in all_coin_markets.values():
        _index(events)
    _index(todays_markets)

    return index


def format_for_claude(all_coin_markets: dict, todays_markets: list) -> str:
    lines = [f"Current time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append("=" * 60)

    now_utc = datetime.now(timezone.utc)

    for section_label, suffix in [("5-MINUTE", "_5m"), ("15-MINUTE", "_15m")]:
        section_markets = {k: v for k, v in all_coin_markets.items() if k.endswith(suffix)}
        if not any(section_markets.values()):
            continue
        lines.append(f"\n## CRYPTO UP/DOWN {section_label} MARKETS")
        lines.append("Fair value without directional signal: ~50/50.\n")
        for coin, events in section_markets.items():
            if not events:
                continue
            coin_label = coin.replace(suffix, "")
            coin_key = coin_label.replace("_", "").lower()  # e.g. "BTC_5m" -> "btc"
            # Binance interval: 1m for 5-min markets, 3m for 15-min markets
            binance_interval = "1m" if suffix == "_5m" else "3m"
            binance_limit = 5
            binance_trend = fetch_binance_trend(coin_key, interval=binance_interval, limit=binance_limit)
            lines.append(f"### {coin_label}")
            if binance_trend:
                lines.append(f"  {binance_trend}")
            for event in events:
                title = event.get("title", "Unknown")
                end_date_raw = event.get("endDate", "")
                # Skip expired markets
                if end_date_raw:
                    try:
                        end_dt = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                        if end_dt <= now_utc:
                            continue
                    except ValueError:
                        pass
                end_date = (end_date_raw or "N/A")[:19].replace("T", " ")
                volume = float(event.get("volume", 0))
                market = event.get("markets", [{}])[0]
                outcomes = _parse_json_field(market.get("outcomes"))
                prices = _parse_json_field(market.get("outcomePrices"))
                token_ids = _parse_json_field(market.get("clobTokenIds"))

                # Window start = end_time minus window duration
                window_duration = 300 if suffix == "_5m" else 900
                window_start = 0
                if end_date_raw:
                    try:
                        end_dt = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                        window_start = int(end_dt.timestamp()) - window_duration
                    except ValueError:
                        pass

                lines.append(f"**{title}**  |  Ends: {end_date} UTC  |  Volume: ${volume:,.0f}")
                for i, (outcome, price) in enumerate(zip(outcomes, prices)):
                    try:
                        pct = float(price) * 100
                        tid = token_ids[i] if i < len(token_ids) else "N/A"
                        trend = fetch_price_trend(tid, window_start=window_start) if tid != "N/A" else ""
                        trend_str = f"  Trend: {trend}" if trend else ""
                        lines.append(f"  {outcome}: {pct:.1f}%  [token_id: {tid}]{trend_str}")
                    except (ValueError, TypeError):
                        pass
                lines.append("")

    # Today's markets section
    lines.append("=" * 60)
    lines.append("\n## TODAY'S SHORT-WINDOW MARKETS (sports, weather, news, crypto levels)\n")

    if not todays_markets:
        lines.append("No qualifying markets found closing within 24h.")
    else:
        for event in todays_markets:
            end_date_raw = event.get("endDate", "")
            if end_date_raw:
                try:
                    end_dt = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                    if end_dt <= now_utc:
                        continue
                except ValueError:
                    pass
            title = event.get("title", "Unknown")
            end_date = (end_date_raw or "N/A")[:19].replace("T", " ")
            volume = float(event.get("volume", 0))
            lines.append(f"**{title}**  |  Ends: {end_date} UTC  |  Volume: ${volume:,.0f}")

            for market in event.get("markets", [])[:4]:
                question = market.get("question", "")
                outcomes = _parse_json_field(market.get("outcomes"))
                prices = _parse_json_field(market.get("outcomePrices"))
                token_ids = _parse_json_field(market.get("clobTokenIds"))
                if not outcomes or not prices:
                    continue
                if question and question != title:
                    lines.append(f"  Q: {question}")
                for i, (outcome, price) in enumerate(zip(outcomes, prices)):
                    try:
                        pct = float(price) * 100
                        tid = token_ids[i] if i < len(token_ids) else "N/A"
                        lines.append(f"  {outcome}: {pct:.1f}%  [token_id: {tid}]")
                    except (ValueError, TypeError):
                        pass
            lines.append("")

    return "\n".join(lines)


# ── Claude analysis ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a sharp prediction market analyst for Polymarket covering two market types:

1. CRYPTO UP/DOWN 5-MINUTE AND 15-MINUTE MARKETS (BTC, ETH, SOL, XRP)
   - Resolve UP if price at window end >= price at window start
   - Fair value without directional signal: exactly 50/50
   - Only flag if price deviates >5 cents from 0.50
   - Each coin section includes TWO trend signals:
     a) "X spot (Binance)" — real-time spot price direction from Binance (1m candles).
        This reflects what is ACTUALLY happening to the crypto price right now.
     b) "Trend:" per token — Polymarket market sentiment/positioning within the window.
   - USE BOTH together to make decisions:
     * Binance FALLING + Polymarket Down leading → momentum confirmed, do NOT fade
     * Binance RISING + Polymarket Down leading → DIVERGENCE, potential edge (Polymarket lagging)
     * Binance FLAT + any Polymarket deviation → stale signal, mean reversion possible
   - Never assign HIGH confidence based on Polymarket deviation alone — always check Binance direction.
   - The divergence case (Binance contradicts Polymarket) is where real alpha lives.

2. TODAY'S SHORT-WINDOW MARKETS (sports, weather, news, crypto price levels)
   - Resolve within 24 hours
   - Fair value varies — use your knowledge of base rates, recent news, and context
   - Flag if you see clear mispricing vs. fair value

Return ONLY a valid JSON array of trade objects — no other text, no markdown.

Each trade object must have:
  token_id        — exact token_id string from the market data
  category        — "crypto-5m" or "todays-market"
  outcome         — outcome label (e.g. "Up", "Down", "Yes", "No", or specific value)
  market_question — the market question text
  side            — "BUY" or "SELL"
  current_price   — current market price as decimal (e.g. 0.42)
  target_price    — your fair value estimate as decimal
  limit_price     — limit order price (slightly better than current to improve fill odds)
  reasoning       — 1-2 sentence explanation
  confidence      — "low", "medium", or "high"

Rules:
- Skip markets where prices are near 0.95+ or 0.05- (near resolution)
- Return 1–5 trades total across both categories, or [] if nothing is compelling
- Do not include any text outside the JSON array
"""


DATA_API     = "https://data-api.polymarket.com"
LESSONS_FILE = "lessons.md"


def load_lessons() -> str:
    """Load lessons.md and format it for the system prompt."""
    if not os.path.exists(LESSONS_FILE):
        return ""
    with open(LESSONS_FILE) as f:
        return "\n\n" + f.read()


def fetch_recent_performance(limit: int = 20) -> str:
    """
    Fetch recent resolved trades from the Data API and format them as a
    performance summary to include in Claude's prompt.
    """
    wallet = os.getenv("POLY_FUNDER", "")
    if not wallet:
        return ""

    try:
        r = requests.get(f"{DATA_API}/activity", params={"user": wallet, "limit": 200}, timeout=10)
        r.raise_for_status()
        activity = r.json()
    except Exception:
        return ""

    # Build a map of recent buys by title+outcome, then match with redeems
    buys: dict[str, list] = {}
    redeems: list[dict] = []

    for a in activity:
        atype = a.get("type", "")
        if atype == "TRADE" and a.get("side") == "BUY":
            key = a.get("title", "") + "|" + a.get("outcome", "")
            buys.setdefault(key, []).append(a)
        elif atype == "REDEEM":
            redeems.append(a)

    results = []
    seen_titles = set()
    for redeem in redeems:
        title = redeem.get("title", "")
        if title in seen_titles:
            continue
        seen_titles.add(title)

        won = float(redeem.get("usdcSize", 0)) > 0
        # Find matching buy
        matching_buy = None
        for key, buy_list in buys.items():
            if key.startswith(title + "|"):
                matching_buy = buy_list[0]
                break

        outcome = matching_buy.get("outcome", "?") if matching_buy else "?"
        side = matching_buy.get("side", "BUY") if matching_buy else "BUY"
        price = float(matching_buy.get("price", 0)) * 100 if matching_buy else 0
        spent = float(matching_buy.get("usdcSize", 0)) if matching_buy else 0
        returned = float(redeem.get("usdcSize", 0))
        pnl = returned - spent

        results.append({
            "title": title,
            "bet": f"{side} {outcome} @ {price:.0f}¢",
            "won": won,
            "pnl": pnl,
        })

        if len(results) >= limit:
            break

    if not results:
        return ""

    wins = sum(1 for r in results if r["won"])
    losses = len(results) - wins
    total_pnl = sum(r["pnl"] for r in results)

    lines = [
        "\n" + "=" * 60,
        "RECENT TRADE PERFORMANCE (learn from these results):",
        f"Last {len(results)} resolved trades: {wins} wins / {losses} losses  |  Net P&L: ${total_pnl:+.2f}",
        "",
    ]
    for r in results:
        tag = "WIN " if r["won"] else "LOSS"
        lines.append(f"  [{tag}] {r['bet']:25s}  P&L: ${r['pnl']:+.2f}  —  {r['title']}")

    lines += [
        "",
        "Use this history to calibrate your confidence. If a market type is",
        "consistently losing, be more skeptical or return [] for those signals.",
        "=" * 60,
    ]
    return "\n".join(lines)


def get_recommendations(markets_text: str) -> list[dict]:  # noqa: keep signature
    import time as _time
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("[ERROR] Set ANTHROPIC_API_KEY in .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    print("[Asking Claude for trade recommendations...]\n")

    performance = fetch_recent_performance(limit=20)
    lessons     = load_lessons()
    full_prompt = markets_text + performance
    system      = SYSTEM_PROMPT + lessons

    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": full_prompt}],
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < max_retries - 1:
                wait = 15 * (attempt + 1)
                print(f"  [Claude overloaded] Retrying in {wait}s... (attempt {attempt + 1}/{max_retries})")
                _time.sleep(wait)
            else:
                print(f"  [Claude API error {e.status_code}] {e.message}")
                return []
    else:
        print("  [Claude unavailable] All retries exhausted.")
        return []

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    cost = (input_tokens / 1_000_000 * 5.00) + (output_tokens / 1_000_000 * 25.00)
    print(f"  Tokens: {input_tokens:,} in / {output_tokens:,} out  |  Cost: ${cost:.4f}\n")

    text = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break

    try:
        trades = json.loads(text)
        if isinstance(trades, list):
            return trades
    except json.JSONDecodeError:
        pass

    # Fallback: extract JSON array from text
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    print(f"[WARNING] Could not parse Claude response as JSON:\n{text[:400]}")
    return []


# ── CLOB client ───────────────────────────────────────────────────────────────

def build_clob_client() -> ClobClient:
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    api_key = os.getenv("POLY_API_KEY", "")
    api_secret = os.getenv("POLY_API_SECRET", "")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

    if not private_key or private_key == "your_wallet_private_key_here":
        print("[ERROR] POLY_PRIVATE_KEY not set. Run setup_trading.py first.")
        sys.exit(1)
    if not api_key:
        print("[ERROR] POLY_API_KEY not set. Run setup_trading.py first.")
        sys.exit(1)
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    funder = os.getenv("POLY_FUNDER", "")
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)

    if funder:
        return ClobClient(
            CLOB_HOST,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=creds,
            signature_type=1,
            funder=funder,
        )
    return ClobClient(
        CLOB_HOST,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        creds=creds,
        signature_type=0,
    )


# ── Approval + execution loop ─────────────────────────────────────────────────

def show_recommendation(i: int, total: int, rec: dict):
    current = float(rec.get("current_price", 0))
    target = float(rec.get("target_price", 0))
    limit = float(rec.get("limit_price", 0))
    edge = (target - current) * 100

    print(f"\n{'═' * 60}")
    print(f"  TRADE {i} of {total}")
    print(f"{'═' * 60}")
    print(f"  Market:     {rec.get('market_question', 'N/A')}")
    print(f"  Outcome:    {rec.get('outcome', 'N/A')}  →  {rec.get('side', 'BUY')}")
    print(f"  Current:    {current*100:.1f}¢   Fair value: {target*100:.1f}¢   Edge: {edge:+.1f}¢")
    print(f"  Limit at:   {limit*100:.1f}¢   Size: ${DEFAULT_ORDER_SIZE:.2f} USDC")
    print(f"  Confidence: {rec.get('confidence', '?').upper()}")
    print(f"  Reasoning:  {rec.get('reasoning', 'N/A')}")
    print(f"  Token ID:   {str(rec.get('token_id', ''))[:40]}...")


def prompt_approval() -> str:
    """Returns 'execute', 'skip', or 'quit'."""
    while True:
        raw = input("\n  [E]xecute  [S]kip  [Q]uit  > ").strip().lower()
        if raw in ("e", "execute"):
            return "execute"
        if raw in ("s", "skip", ""):
            return "skip"
        if raw in ("q", "quit"):
            return "quit"
        print("  Please enter E, S, or Q.")


PAPER_TRADES_FILE = "paper_trades.json"


def paper_execute(rec: dict):
    """Log a simulated trade to paper_trades.json instead of hitting the CLOB."""
    limit_price = float(rec.get("limit_price", 0))
    order_size = rec.get("_override_size", DEFAULT_ORDER_SIZE)
    num_shares = round(order_size / limit_price, 2)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "token_id": rec.get("token_id"),
        "market_question": rec.get("market_question"),
        "outcome": rec.get("outcome"),
        "side": rec.get("side", "BUY").upper(),
        "limit_price": limit_price,
        "num_shares": num_shares,
        "size_usdc": order_size,
        "target_price": rec.get("target_price"),
        "confidence": rec.get("confidence"),
        "reasoning": rec.get("reasoning"),
        "status": "open",
        "fill_price": None,
        "resolved_price": None,
        "pnl_usdc": None,
    }

    trades = []
    if os.path.exists(PAPER_TRADES_FILE):
        with open(PAPER_TRADES_FILE) as f:
            trades = json.load(f)

    trades.append(entry)

    with open(PAPER_TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

    print(f"  ✓ [PAPER] Logged {entry['side']} {num_shares} shares @ {limit_price*100:.1f}¢")
    print(f"    Saved to {PAPER_TRADES_FILE}")


def execute_trade(rec: dict, client: ClobClient):
    token_id = rec.get("token_id")
    side = rec.get("side", "BUY").upper()
    limit_price = float(rec.get("limit_price", 0))
    order_size = rec.get("_override_size", DEFAULT_ORDER_SIZE)

    if not (0.01 <= limit_price <= 0.99):
        print(f"  [ERROR] Limit price {limit_price} out of range, skipping.")
        return

    if order_size > MAX_ORDER_SIZE:
        print(f"  [ERROR] Order size exceeds cap ${MAX_ORDER_SIZE}, skipping.")
        return

    num_shares = round(order_size / limit_price, 2)

    print(f"\n  Placing {side} {num_shares} shares @ {limit_price*100:.1f}¢ ...")

    order_args = OrderArgs(token_id=token_id, price=limit_price, size=num_shares, side=side)
    signed = client.create_order(order_args)

    try:
        response = client.post_order(signed, OrderType.GTC)
    except Exception as e:
        msg = f"⚠️ Order failed: {e}"
        print(f"  [ERROR] {msg}")
        telegram_send(msg)
        return

    order_id = response.get("orderID") or response.get("id", "unknown")
    status = response.get("status", "unknown")
    print(f"  ✓ Order placed!  ID: {order_id}  Status: {status}")

    # Log to live_trades.json
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order_id": order_id,
        "order_status": status,
        "token_id": token_id,
        "market_question": rec.get("market_question"),
        "outcome": rec.get("outcome"),
        "side": side,
        "limit_price": limit_price,
        "num_shares": num_shares,
        "size_usdc": order_size,
        "target_price": rec.get("target_price"),
        "confidence": rec.get("confidence"),
        "reasoning": rec.get("reasoning"),
        "resolved_price": None,
        "pnl_usdc": None,
    }
    live_log = []
    if os.path.exists("live_trades.json"):
        with open("live_trades.json") as f:
            live_log = json.load(f)
    live_log.append(entry)
    with open("live_trades.json", "w") as f:
        json.dump(live_log, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    paper_mode = "--paper" in sys.argv
    auto_mode  = "--auto"  in sys.argv

    print("=" * 60)
    print("  POLYMARKET SEMI-AUTO BOT" + ("  [PAPER MODE]" if paper_mode else "") + ("  [AUTO MODE]" if auto_mode else ""))
    print(f"  Order size: ${DEFAULT_ORDER_SIZE} USDC  |  Cap: ${MAX_ORDER_SIZE} USDC")
    print("=" * 60)

    # 1. Fetch markets
    print("\nFetching markets...")
    all_coin_markets = fetch_all_coins(limit=5)
    todays_markets = fetch_todays_markets(limit=10)
    print(f"  ✓ Today's markets: {len(todays_markets)} qualifying")

    # 2. Build token index for validation
    market_index = build_market_index(all_coin_markets, todays_markets)
    print(f"  ✓ {len(market_index)} tradeable tokens indexed")

    # 3. Get Claude's recommendations
    markets_text = format_for_claude(all_coin_markets, todays_markets)
    recommendations = get_recommendations(markets_text)

    if not recommendations:
        print("\n  No compelling trades found. Try again later.")
        return

    # Validate token IDs are from fetched markets, HIGH confidence only
    valid = []
    for rec in recommendations:
        tid = rec.get("token_id")
        if rec.get("confidence", "").lower() not in ("high", "medium"):
            print(f"  [SKIP] Dropping low confidence trade: {rec.get('market_question', '')[:50]}")
            continue
        if tid and tid in market_index:
            valid.append(rec)
        else:
            print(f"  [WARN] Dropping rec with unrecognized token_id: {str(tid)[:40]}")

    if not valid:
        print("\n  No valid recommendations after token validation.")
        return

    print(f"\n  Claude found {len(valid)} trade(s). Review each below.\n")
    _notify(f"Polymarket Bot: {len(valid)} trade opportunity found!" if len(valid) == 1 else f"Polymarket Bot: {len(valid)} trade opportunities found!")

    # 4. Review and execute
    clob_client = None
    executed = skipped = 0

    for i, rec in enumerate(valid, 1):
        show_recommendation(i, len(valid), rec)
        confidence = rec.get("confidence", "").lower()
        if auto_mode and confidence == "high":
            print("  [AUTO] HIGH confidence — executing automatically.")
            telegram_send(f"⚡ AUTO-EXECUTING high confidence trade: {rec.get('market_question', '')[:60]}")
            action = "execute"
        else:
            action = telegram_ask_approval(rec)

        if action == "quit":
            print("\n  Exiting.")
            telegram_send("🛑 Melissa stopped.")
            break
        elif action == "skip":
            skipped += 1
            print("  Skipped.")
        elif action == "execute":
            if paper_mode:
                paper_execute(rec)
            else:
                if clob_client is None:
                    clob_client = build_clob_client()
                execute_trade(rec, clob_client)
            executed += 1

    summary = f"Session done — {executed} executed, {skipped} skipped"
    print(f"\n{'═' * 60}")
    print(f"  {summary}")
    print("═" * 60 + "\n")
    telegram_send(f"📋 {summary}")


def _notify(message: str):
    """Send a macOS native notification and a Telegram message."""
    import subprocess
    script = f'display notification "{message}" with title "Polymarket Bot" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True)
    telegram_send(f"🤖 {message}")


def telegram_send(message: str):
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def _tg_get_offset() -> int:
    """Return the next update_id offset so we ignore messages sent before now."""
    if not TELEGRAM_TOKEN:
        return 0
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"limit": 100, "timeout": 0},
            timeout=10,
        )
        updates = r.json().get("result", [])
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return 0


def telegram_ask_approval(rec: dict, timeout_seconds: int = 300) -> str:
    """
    Send trade details to Telegram and wait for E / S / Q reply.
    Falls back to terminal input if Telegram is not configured.
    Auto-skips after timeout_seconds with no reply.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return prompt_approval()

    current = float(rec.get("current_price", 0))
    target  = float(rec.get("target_price", 0))
    limit   = float(rec.get("limit_price", 0))
    edge    = (target - current) * 100
    size    = rec.get("_override_size", DEFAULT_ORDER_SIZE)

    msg = (
        f"🤖 <b>MELISSA — TRADE FOUND</b>\n\n"
        f"📊 <b>{rec.get('market_question', 'N/A')}</b>\n"
        f"Outcome: {rec.get('outcome')}  →  {rec.get('side', 'BUY')}\n"
        f"Current: {current*100:.1f}¢  |  Fair value: {target*100:.1f}¢  |  Edge: {edge:+.1f}¢\n"
        f"Limit: {limit*100:.1f}¢  |  Size: ${size:.2f} USDC\n"
        f"Confidence: {rec.get('confidence', '?').upper()}\n\n"
        f"💬 {rec.get('reasoning', '')}\n\n"
        f"Reply <b>E</b> execute  ·  <b>S</b> skip  ·  <b>Q</b> quit\n"
        f"(Auto-skips in {timeout_seconds // 60} min)"
    )

    offset = _tg_get_offset()
    telegram_send(msg)

    url      = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        wait = min(30, int(deadline - time.time()))
        if wait <= 0:
            break
        try:
            r = requests.get(url, params={"offset": offset, "timeout": wait, "limit": 5}, timeout=wait + 5)
            updates = r.json().get("result", [])
        except Exception:
            continue

        for update in updates:
            offset  = update["update_id"] + 1
            chat_id = str(update.get("message", {}).get("chat", {}).get("id", ""))
            text    = update.get("message", {}).get("text", "").strip().lower()

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue
            if text in ("e", "execute"):
                telegram_send("✅ Executing trade...")
                return "execute"
            if text in ("s", "skip", ""):
                telegram_send("⏭ Skipped.")
                return "skip"
            if text in ("q", "quit"):
                telegram_send("🛑 Stopping Melissa.")
                return "quit"

    telegram_send("⏰ No reply — skipping trade.")
    return "skip"


DEVIATION_THRESHOLD = 0.08  # minimum deviation from 50/50 to trigger Claude
COOLDOWN_MINUTES = 30       # minutes to wait after a no-trade Claude call


def check_deviations(all_coin_markets: dict) -> list[dict]:
    """
    Stage 1 — free check. Scan all markets for price deviations from 50/50.
    Returns list of deviating markets with coin, title, outcome, price, deviation.
    No Claude call.
    """
    deviations = []
    for coin, events in all_coin_markets.items():
        for event in events:
            market = event.get("markets", [{}])[0]
            outcomes = _parse_json_field(market.get("outcomes"))
            prices = _parse_json_field(market.get("outcomePrices"))
            token_ids = _parse_json_field(market.get("clobTokenIds"))

            for i, (outcome, price) in enumerate(zip(outcomes, prices)):
                try:
                    p = float(price)
                    deviation = abs(p - 0.5)
                    if deviation >= DEVIATION_THRESHOLD:
                        deviations.append({
                            "coin": coin,
                            "title": event.get("title", ""),
                            "end_date": event.get("endDate", "")[:19].replace("T", " "),
                            "outcome": outcome,
                            "price": p,
                            "deviation": deviation,
                            "token_id": token_ids[i] if i < len(token_ids) else None,
                        })
                except (ValueError, TypeError):
                    pass
    return deviations


PID_FILE = "/tmp/melissa_bot.pid"

def acquire_pid_lock():
    """Prevent duplicate Melissa instances. Exits if another instance is already running."""
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            old_pid = f.read().strip()
        try:
            os.kill(int(old_pid), 0)  # signal 0 = check if process exists
            print(f"[ERROR] Melissa is already running (PID {old_pid}). Refusing to start a duplicate.")
            print(f"        Kill it first: kill {old_pid}")
            sys.exit(1)
        except (OSError, ValueError):
            pass  # old PID is dead — stale lock file, safe to overwrite

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def release_pid_lock():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def loop(interval_seconds: int = 300):
    import time
    paper_mode = "--paper" in sys.argv
    auto_mode  = "--auto"  in sys.argv

    acquire_pid_lock()

    print("=" * 60)
    print(f"  POLYMARKET BOT — SMART LOOP {'[PAPER] ' if paper_mode else ''}{'[AUTO] ' if auto_mode else ''}")
    print(f"  Scanning every {interval_seconds}s | Claude only on deviation >{DEVIATION_THRESHOLD*100:.0f}¢ + high-vol today's markets")
    print(f"  Cooldown after no-trade call: {COOLDOWN_MINUTES}min")
    print("=" * 60)

    scan = 0
    last_claude_call = None  # track when Claude was last called with no result

    while True:
        scan += 1
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{'─' * 60}")
        print(f"  Scan #{scan} — {now_str}")
        print(f"{'─' * 60}")

        # Cooldown check — skip Claude if recently called with no trades
        if last_claude_call:
            mins_since = (now - last_claude_call).total_seconds() / 60
            if mins_since < COOLDOWN_MINUTES:
                print(f"  Cooldown active — {COOLDOWN_MINUTES - mins_since:.0f}min remaining. Skipping Claude.")
                print(f"\n  Next scan in {interval_seconds}s... (Ctrl+C to stop)")
                time.sleep(interval_seconds)
                continue

        # Stage 1: free price fetch + deviation check
        all_coin_markets = fetch_all_coins(limit=5)
        todays_markets = fetch_todays_markets(limit=10)
        deviations = check_deviations(all_coin_markets)

        # Only trigger Claude when BOTH crypto deviates AND high-vol today's markets exist
        trigger = deviations and todays_markets
        if not trigger:
            if not deviations:
                print(f"  All crypto within {DEVIATION_THRESHOLD*100:.0f}¢ of 50/50 — skipping Claude.")
            elif not todays_markets:
                print("  No high-volume today's markets — skipping Claude.")
        else:
            print(f"  {len(deviations)} crypto deviation(s) detected:")
            for d in deviations:
                print(f"    {d['coin']} {d['outcome']} @ {d['price']*100:.1f}¢  (dev: {d['deviation']*100:+.1f}¢)  ends {d['end_date']} UTC")
            print(f"  {len(todays_markets)} high-volume today's market(s) included")
            print("  Calling Claude...")

            # Stage 2: Claude analysis
            market_index = build_market_index(all_coin_markets, todays_markets)
            markets_text = format_for_claude(all_coin_markets, todays_markets)
            recommendations = get_recommendations(markets_text)

            valid = [r for r in recommendations if r.get("token_id") in market_index and r.get("confidence", "").lower() in ("high", "medium")]
            if not valid:
                print("  Claude found no actionable trades — starting cooldown.")
                last_claude_call = now
            else:
                last_claude_call = None  # reset cooldown on a successful find
                _notify(f"{len(valid)} trade {'opportunity' if len(valid) == 1 else 'opportunities'} found!")
                print(f"  Claude found {len(valid)} trade(s). Review each below.\n")

                clob_client = None
                executed = skipped = 0

                for i, rec in enumerate(valid, 1):
                    show_recommendation(i, len(valid), rec)
                    confidence = rec.get("confidence", "").lower()
                    if auto_mode and confidence == "high":
                        print("  [AUTO] HIGH confidence — executing automatically.")
                        telegram_send(f"⚡ AUTO-EXECUTING high confidence trade: {rec.get('market_question', '')[:60]}")
                        action = "execute"
                    elif auto_mode and confidence == "medium":
                        print("  [AUTO] MEDIUM confidence — asking via Telegram.")
                        rec["_override_size"] = DEFAULT_ORDER_SIZE / 2
                        action = telegram_ask_approval(rec)
                    elif auto_mode:
                        print("  [AUTO] LOW confidence — skipping.")
                        action = "skip"
                    else:
                        action = telegram_ask_approval(rec)

                    if action == "quit":
                        print("\n  Exiting loop.")
                        telegram_send("🛑 Melissa stopped.")
                        return
                    elif action == "skip":
                        skipped += 1
                        print("  Skipped.")
                    elif action == "execute":
                        if paper_mode:
                            paper_execute(rec)
                        else:
                            if clob_client is None:
                                clob_client = build_clob_client()
                            execute_trade(rec, clob_client)
                        executed += 1

                session_summary = f"{executed} executed, {skipped} skipped"
                print(f"\n  Session: {session_summary}")
                telegram_send(f"📋 Session: {session_summary}")

        print(f"\n  Next scan in {interval_seconds}s... (Ctrl+C to stop)")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        try:
            loop(interval_seconds=300)
        finally:
            release_pid_lock()
    else:
        main()
