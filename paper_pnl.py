"""
Review paper trading performance.

Fetches current/resolved prices for all paper trades and shows P&L.

Usage:
  python3 paper_pnl.py           — show summary (updates prices automatically)
  python3 paper_pnl.py --detail  — show full detail per trade
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

PAPER_TRADES_FILE = "paper_trades.json"
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


# ── Price fetching ────────────────────────────────────────────────────────────

def fetch_current_price(token_id: str) -> float | None:
    """Return the current midpoint price for a token (0.0–1.0). No auth needed."""
    try:
        r = requests.get(f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        data = r.json()
        mid = data.get("mid")
        if mid is not None:
            return float(mid)
    except Exception:
        pass
    return None


def fetch_resolved_price(token_id: str) -> float | None:
    """
    Check if the market resolved. Returns 1.0 (won), 0.0 (lost), or None (still open).
    Uses the Gamma API — resolved markets have active=false and outcomePrices of 0 or 1.
    """
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={
            "clob_token_ids": token_id, "limit": 1,
        }, timeout=8)
        r.raise_for_status()
        markets = r.json()

        if not markets or not isinstance(markets, list):
            return None

        market = markets[0]
        active = market.get("active", True)
        closed = market.get("closed", False)

        if not active or closed:
            token_ids = market.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            prices = market.get("outcomePrices", [])
            if isinstance(prices, str):
                prices = json.loads(prices)

            if token_id in token_ids:
                idx = token_ids.index(token_id)
                if idx < len(prices):
                    return float(prices[idx])
    except Exception:
        pass
    return None


# ── P&L calculation ───────────────────────────────────────────────────────────

def calc_pnl(trade: dict, current_price: float) -> float:
    """
    P&L in USDC for a BUY trade given current/resolved price.
    P&L = (current_price - entry_price) * num_shares
    """
    side = trade.get("side", "BUY").upper()
    entry = float(trade.get("limit_price", 0))
    shares = float(trade.get("num_shares", 0))

    if side == "BUY":
        return (current_price - entry) * shares
    else:  # SELL
        return (entry - current_price) * shares


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    detail = "--detail" in sys.argv

    if not os.path.exists(PAPER_TRADES_FILE):
        print(f"\n  No paper trades found. Run:  python3 bot.py --paper\n")
        return

    with open(PAPER_TRADES_FILE) as f:
        trades = json.load(f)

    if not trades:
        print("\n  paper_trades.json is empty.\n")
        return

    print("=" * 70)
    print("  PAPER TRADING P&L")
    print(f"  {len(trades)} trade(s) total  |  fetching current prices...")
    print("=" * 70)

    total_pnl = 0.0
    total_invested = 0.0
    resolved_count = 0
    open_count = 0

    updated_trades = []

    for i, trade in enumerate(trades, 1):
        token_id = trade.get("token_id", "")
        entry_price = float(trade.get("limit_price", 0))
        num_shares = float(trade.get("num_shares", 0))
        size_usdc = float(trade.get("size_usdc", 0))
        side = trade.get("side", "BUY").upper()
        question = trade.get("market_question", "N/A")
        outcome = trade.get("outcome", "N/A")
        confidence = trade.get("confidence", "?").upper()
        timestamp = trade.get("timestamp", "")[:16].replace("T", " ")

        # Check resolved first, then fall back to current midpoint
        resolved = fetch_resolved_price(token_id)
        if resolved is not None:
            current_price = resolved
            status = "RESOLVED"
            resolved_count += 1
            trade["status"] = "resolved"
            trade["resolved_price"] = resolved
        else:
            current_price = fetch_current_price(token_id)
            if current_price is None:
                status = "NO PRICE"
                open_count += 1
            else:
                status = "OPEN"
                open_count += 1
            trade["status"] = "open"

        if current_price is not None:
            pnl = calc_pnl(trade, current_price)
            trade["pnl_usdc"] = round(pnl, 4)
        else:
            pnl = None
            trade["pnl_usdc"] = None

        updated_trades.append(trade)

        if current_price is not None:
            total_pnl += pnl
            total_invested += size_usdc

        # Always print the trade
        pnl_str = f"{pnl:+.2f}" if pnl is not None else "  N/A"
        price_str = f"{current_price*100:.1f}¢" if current_price is not None else "  N/A"
        print(
            f"\n  [{i:02d}] {status:<10} {timestamp}  conf={confidence}"
            f"\n       {side} {outcome} | entry {entry_price*100:.1f}¢ → now {price_str}"
            f"   P&L: ${pnl_str} USDC"
        )

        if detail:
            print(f"       Q: {question}")
            print(f"       Reasoning: {trade.get('reasoning', 'N/A')}")

    # Save updated trades
    with open(PAPER_TRADES_FILE, "w") as f:
        json.dump(updated_trades, f, indent=2)

    # Summary
    print(f"\n{'═' * 70}")
    print(f"  SUMMARY")
    print(f"{'─' * 70}")
    print(f"  Total trades:   {len(trades)}  ({resolved_count} resolved, {open_count} open)")
    if total_invested > 0:
        roi = (total_pnl / total_invested) * 100
        print(f"  Total invested: ${total_invested:.2f} USDC")
        print(f"  Total P&L:      ${total_pnl:+.2f} USDC  ({roi:+.1f}% ROI)")
    print(f"  (paper_trades.json updated with latest prices)")
    print("═" * 70 + "\n")


if __name__ == "__main__":
    main()
