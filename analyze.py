"""
Claude AI analysis of live Polymarket markets.

Covers:
- Geopolitics: Trump, Iran, Israel, Middle East, Indonesia
- BTC Up/Down 5-minute markets (short-term price movement)
"""

import json
import os
import sys
from datetime import datetime, timezone

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

GAMMA_API = "https://gamma-api.polymarket.com"

GEOPOLITICS_TOPICS = {
    "trump": "trump",
    "iran": "iran",
    "israel": "israel",
    "middle east": "middle-east",
    "geopolitics": "geopolitics",
    "indonesia": "indonesia",
}


# ── Polymarket helpers ────────────────────────────────────────────────────────

def fetch_events(tag_slug: str, limit: int = 6) -> list[dict]:
    params = {
        "tag_slug": tag_slug,
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume",
        "ascending": "false",
    }
    r = requests.get(f"{GAMMA_API}/events", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_btc_updown_5m(limit: int = 10) -> list[dict]:
    """
    Fetch active BTC Up/Down 5-minute markets, sorted by soonest end time.
    Filters out events whose prices are missing (already resolved/stale).
    """
    r = requests.get(f"{GAMMA_API}/events", params={
        "tag_slug": "bitcoin",
        "active": "true",
        "closed": "false",
        "limit": 50,
    }, timeout=10)
    r.raise_for_status()

    now = datetime.now(timezone.utc)
    results = []

    for event in r.json():
        if not isinstance(event, dict):
            continue
        ticker = event.get("ticker", "")
        if "updown" not in ticker.lower():
            continue

        # Parse endDate and skip already-ended events
        end_raw = event.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if end_dt <= now:
            continue

        # Only include if prices are available
        markets = event.get("markets", [])
        if not markets:
            continue
        prices = markets[0].get("outcomePrices")
        if not prices:
            # Try fetching this event individually by slug to get prices
            try:
                r2 = requests.get(f"{GAMMA_API}/events", params={"slug": event["slug"]}, timeout=5)
                detail = r2.json()
                if detail and isinstance(detail[0], dict):
                    prices = detail[0].get("markets", [{}])[0].get("outcomePrices")
                    if prices:
                        event = detail[0]
            except Exception:
                pass

        if not prices:
            continue

        results.append(event)
        if len(results) >= limit:
            break

    # Sort by soonest end time
    results.sort(key=lambda e: e.get("endDate", ""))
    return results


def format_markets_for_claude(all_events: dict[str, list], btc5m: list[dict]) -> str:
    lines = [f"Today's date/time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append("=" * 60)

    # Geopolitics section
    lines.append("\n## GEOPOLITICS MARKETS\n")
    for topic, events in all_events.items():
        if not events:
            continue
        lines.append(f"### {topic.upper()}")
        for event in events:
            title = event.get("title", "Unknown")
            volume = float(event.get("volume", 0))
            end_date = (event.get("endDate") or "N/A")[:10]
            markets = event.get("markets", [])

            lines.append(f"\n**{title}**")
            lines.append(f"Volume: ${volume:,.0f} | Closes: {end_date}")

            for market in markets[:4]:
                question = market.get("question", "")
                outcomes = market.get("outcomes", [])
                prices = market.get("outcomePrices", [])

                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if isinstance(prices, str):
                    prices = json.loads(prices)

                if not question or not outcomes or not prices:
                    continue

                lines.append(f"  Q: {question}")
                for outcome, price in zip(outcomes, prices):
                    try:
                        pct = float(price) * 100
                        lines.append(f"     {outcome}: {pct:.1f}%")
                    except (ValueError, TypeError):
                        pass
        lines.append("")

    # BTC Up/Down 5-minute section
    lines.append("=" * 60)
    lines.append("\n## BTC UP/DOWN 5-MINUTE MARKETS\n")
    lines.append("These markets resolve UP if BTC price at end of window >= price at start.")
    lines.append("A fair market with no trend should be ~50/50. Deviations suggest momentum or mispricing.\n")

    if not btc5m:
        lines.append("No active BTC up/down 5-minute markets found right now.")
    else:
        for event in btc5m:
            title = event.get("title", "Unknown")
            end_date = event.get("endDate", "N/A")[:19].replace("T", " ")
            volume = float(event.get("volume", 0))
            liquidity = float(event.get("liquidity", 0))
            market = event.get("markets", [{}])[0]

            outcomes = market.get("outcomes", [])
            prices = market.get("outcomePrices", [])
            best_bid = market.get("bestBid")
            best_ask = market.get("bestAsk")
            last_trade = market.get("lastTradePrice")

            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)

            lines.append(f"**{title}**")
            lines.append(f"Ends: {end_date} UTC | Volume: ${volume:,.0f} | Liquidity: ${liquidity:,.0f}")
            if outcomes and prices:
                for outcome, price in zip(outcomes, prices):
                    try:
                        pct = float(price) * 100
                        lines.append(f"  {outcome}: {pct:.1f}%")
                    except (ValueError, TypeError):
                        pass
            if last_trade:
                lines.append(f"  Last trade (Up): {float(last_trade)*100:.1f}%")
            if best_bid and best_ask:
                lines.append(f"  Best bid/ask (Up): {float(best_bid)*100:.1f}% / {float(best_ask)*100:.1f}%")
            lines.append("")

    return "\n".join(lines)


# ── Claude analysis ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a sharp prediction market analyst. You cover two areas:

1. GEOPOLITICS — Trump administration actions, US-Iran relations, Israel/Gaza/Lebanon
   conflict, Indonesian politics. Flag mispricings by referencing specific recent events,
   base rates, or structural factors.

2. BTC UP/DOWN 5-MINUTE MARKETS — These are binary markets resolving UP if BTC price
   at window end >= price at window start. Fair value without trend is ~50/50.
   Flag markets where the current price deviates meaningfully from 50/50 and explain
   whether that deviation is justified (strong momentum) or a mispricing opportunity.
   Note: prices near 95%+ or 5%- are often near resolution, not mispricings.

For each flagged market:
- State the current price and what you think fair value is
- Explain your reasoning concisely (1-3 sentences)
- Rate confidence: low / medium / high

Skip correctly-priced markets. Be direct and analytical.
Do NOT frame anything as financial advice.
"""


def analyze_with_claude(markets_text: str) -> None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your_key_here":
        print("\n[ERROR] Set your ANTHROPIC_API_KEY in .env first.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("\n" + "=" * 60)
    print("  CLAUDE AI MARKET ANALYSIS")
    print("  Model: claude-opus-4-6 | Adaptive Thinking ON")
    print("=" * 60 + "\n")

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{markets_text}\n\n"
                    "Identify the most interesting mispricings across both the "
                    "geopolitics and BTC 5-minute markets. Top 3-5 picks with reasoning."
                ),
            }
        ],
    ) as stream:
        thinking_shown = False

        for event in stream:
            if event.type == "content_block_start":
                if event.content_block.type == "thinking":
                    print("[Claude is thinking...]\n")
                    thinking_shown = True
                elif event.content_block.type == "text" and thinking_shown:
                    print("\n[Analysis]\n" + "-" * 40)

            elif event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    print(event.delta.text, end="", flush=True)

        final = stream.get_final_message()
        input_tokens = final.usage.input_tokens
        output_tokens = final.usage.output_tokens

    print(f"\n\n{'─' * 60}")
    print(f"  Tokens used: {input_tokens:,} in / {output_tokens:,} out")
    cost = (input_tokens / 1_000_000 * 5.00) + (output_tokens / 1_000_000 * 25.00)
    print(f"  Estimated cost: ${cost:.4f}")
    print("=" * 60 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching live markets from Polymarket...")

    all_events = {}
    for label, slug in GEOPOLITICS_TOPICS.items():
        try:
            all_events[label] = fetch_events(tag_slug=slug, limit=6)
            print(f"  ✓ {label}: {len(all_events[label])} events")
        except requests.RequestException as e:
            print(f"  ✗ {label}: {e}")
            all_events[label] = []

    print("  Fetching BTC up/down 5-minute markets...")
    btc5m = fetch_btc_updown_5m(limit=10)
    print(f"  ✓ BTC 5m: {len(btc5m)} active markets")

    markets_text = format_markets_for_claude(all_events, btc5m)
    analyze_with_claude(markets_text)


if __name__ == "__main__":
    main()
