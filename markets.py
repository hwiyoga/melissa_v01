"""
Stage 2: Fetch live Polymarket markets via the Gamma API.
No API key needed — this is all public data.

The Gamma API organizes markets into "events" (groups of related markets).
Events have tags (e.g. "politics", "crypto", "elections").
"""

import json
import requests

GAMMA_API = "https://gamma-api.polymarket.com"

# Topics we care about — must match Polymarket's tag slugs exactly
TOPICS_OF_INTEREST = {
    "politics": "politics",
    "elections": "elections",
    "crypto": "crypto",
    "current events": "current-events",
}


def fetch_events_by_tag(tag_slug: str, limit: int = 5) -> list[dict]:
    """
    Fetch active events (market groups) filtered by tag.
    Each event can contain multiple markets.
    """
    params = {
        "tag_slug": tag_slug,
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume",
        "ascending": "false",
    }
    response = requests.get(f"{GAMMA_API}/events", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def print_event(event: dict) -> None:
    """Pretty-print an event and its top markets."""
    title = event.get("title", "Unknown")
    volume = float(event.get("volume", 0))
    end_date = (event.get("endDate") or "N/A")[:10]
    markets = event.get("markets", [])

    print(f"\n  [{title}]")
    print(f"  Event volume: ${volume:,.0f}  |  Closes: {end_date}")

    for market in markets[:3]:  # show up to 3 sub-markets per event
        question = market.get("question", "")
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])

        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)

        print(f"\n    Q: {question}")
        if outcomes and prices:
            for outcome, price in zip(outcomes, prices):
                try:
                    pct = float(price) * 100
                    print(f"       {outcome}: {pct:.1f}%")
                except (ValueError, TypeError):
                    pass


def main():
    print("=" * 60)
    print("  POLYMARKET — LIVE MARKETS")
    print("=" * 60)

    for label, tag_slug in TOPICS_OF_INTEREST.items():
        print(f"\n{'─' * 60}")
        print(f"  TOPIC: {label.upper()}")
        print(f"{'─' * 60}")

        try:
            events = fetch_events_by_tag(tag_slug=tag_slug, limit=5)
            if not events:
                print("  No active events found for this topic.")
                continue
            for event in events:
                print_event(event)
        except requests.RequestException as e:
            print(f"  Error fetching {label}: {e}")

    print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
