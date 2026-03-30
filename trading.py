"""
Stage 4: Place trades on Polymarket via the CLOB API.

Usage:
  python3 trading.py balance              — show USDC balance
  python3 trading.py search <keyword>     — find markets by keyword
  python3 trading.py orders               — list your open orders
  python3 trading.py cancel <order_id>    — cancel one order
  python3 trading.py cancel all           — cancel all open orders
  python3 trading.py buy  <token_id> <price> <size_usdc>
  python3 trading.py sell <token_id> <price> <size_usdc>

  Add --confirm to actually place/cancel orders.
  Without --confirm everything runs in DRY RUN mode (safe to experiment).

Examples:
  python3 trading.py balance
  python3 trading.py search "bitcoin"
  python3 trading.py buy 0xabc...123 0.35 5 --confirm
     → buys $5 of YES tokens at 35¢ each

Safety limits (edit below to change):
  MAX_ORDER_USDC = 25    never place an order larger than $25
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    TradeParams,
)

load_dotenv()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
POLYGON_CHAIN_ID = 137

# Safety cap — orders larger than this are rejected before sending
MAX_ORDER_USDC = 25.0


# ── Client setup ─────────────────────────────────────────────────────────────

def build_client() -> ClobClient:
    private_key = os.getenv("POLY_PRIVATE_KEY", "")
    api_key = os.getenv("POLY_API_KEY", "")
    api_secret = os.getenv("POLY_API_SECRET", "")
    api_passphrase = os.getenv("POLY_API_PASSPHRASE", "")

    if not private_key or private_key == "your_wallet_private_key_here":
        print("[ERROR] POLY_PRIVATE_KEY not set. Run python3 setup_trading.py first.")
        sys.exit(1)

    if not api_key:
        print("[ERROR] POLY_API_KEY not set. Run python3 setup_trading.py first.")
        sys.exit(1)

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    funder = os.getenv("POLY_FUNDER", "")
    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )

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


# ── Balance ───────────────────────────────────────────────────────────────────

def cmd_balance():
    client = build_client()
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    result = client.get_balance_allowance(params)

    balance_raw = result.get("balance", "0")
    allowance_raw = result.get("allowance", "0")

    # USDC has 6 decimal places on Polygon
    balance = int(balance_raw) / 1_000_000
    allowance = int(allowance_raw) / 1_000_000

    print(f"\n  USDC Balance:   ${balance:,.2f}")
    print(f"  USDC Allowance: ${allowance:,.2f}  (approved for trading)\n")


# ── Market search ─────────────────────────────────────────────────────────────

def cmd_search(keyword: str):
    """Find markets by keyword and show their token IDs (needed for trading)."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": 10,
        "order": "volume",
        "ascending": "false",
    }
    r = requests.get(f"{GAMMA_API}/markets", params=params, timeout=10)
    r.raise_for_status()
    markets = r.json()

    keyword_lower = keyword.lower()
    matches = [m for m in markets if keyword_lower in m.get("question", "").lower()]

    if not matches:
        # Broader search via events
        r2 = requests.get(
            f"{GAMMA_API}/markets",
            params={**params, "limit": 50},
            timeout=10,
        )
        all_markets = r2.json()
        matches = [
            m for m in all_markets
            if keyword_lower in m.get("question", "").lower()
        ]

    if not matches:
        print(f"\n  No active markets found matching '{keyword}'\n")
        return

    print(f"\n  Markets matching '{keyword}':\n")
    for m in matches[:8]:
        question = m.get("question", "")
        volume = float(m.get("volume", 0))
        token_ids = m.get("clobTokenIds", [])
        outcomes = m.get("outcomes", [])
        prices = m.get("outcomePrices", [])

        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)

        print(f"  Q: {question}")
        print(f"     Volume: ${volume:,.0f}")

        for i, (outcome, token_id) in enumerate(zip(outcomes, token_ids)):
            price = ""
            if prices and i < len(prices):
                try:
                    price = f"  current: {float(prices[i])*100:.1f}¢"
                except (ValueError, TypeError):
                    pass
            print(f"     [{outcome}] token_id: {token_id}{price}")
        print()


# ── Open orders ───────────────────────────────────────────────────────────────

def cmd_orders():
    client = build_client()
    orders = client.get_orders()

    if not orders:
        print("\n  No open orders.\n")
        return

    print(f"\n  Open orders ({len(orders)}):\n")
    for o in orders:
        side = o.get("side", "?").upper()
        price = float(o.get("price", 0)) * 100
        size = float(o.get("original_size", 0))
        filled = float(o.get("size_matched", 0))
        order_id = o.get("id", "")
        print(
            f"  {side} {size:.2f} tokens @ {price:.1f}¢  "
            f"(filled: {filled:.2f})  id: {order_id}"
        )
    print()


# ── Cancel ────────────────────────────────────────────────────────────────────

def cmd_cancel(order_id: str, confirm: bool):
    if order_id.lower() == "all":
        print(f"\n  {'[DRY RUN] ' if not confirm else ''}Cancelling ALL open orders...")
        if confirm:
            client = build_client()
            client.cancel_all()
            print("  Done.\n")
        else:
            print("  Add --confirm to actually cancel.\n")
    else:
        print(f"\n  {'[DRY RUN] ' if not confirm else ''}Cancelling order {order_id}...")
        if confirm:
            client = build_client()
            client.cancel(order_id)
            print("  Done.\n")
        else:
            print("  Add --confirm to actually cancel.\n")


# ── Place order ───────────────────────────────────────────────────────────────

def cmd_order(side: str, token_id: str, price: float, size_usdc: float, confirm: bool):
    """
    Place a limit order.

    side      : "BUY" or "SELL"
    token_id  : from `trading.py search <keyword>`
    price     : 0.01 – 0.99  (e.g. 0.35 = 35¢ per share)
    size_usdc : dollar amount to spend (e.g. 5 = $5)
    """
    # Validate
    if not 0.01 <= price <= 0.99:
        print(f"[ERROR] Price must be between 0.01 and 0.99 (got {price})")
        sys.exit(1)

    if size_usdc > MAX_ORDER_USDC:
        print(f"[ERROR] Order size ${size_usdc} exceeds safety cap ${MAX_ORDER_USDC}.")
        print(f"  Edit MAX_ORDER_USDC in trading.py to raise the limit.")
        sys.exit(1)

    # Number of shares = dollars / price per share
    num_shares = round(size_usdc / price, 2)
    total_cost = round(num_shares * price, 2)

    print(f"\n  {'[DRY RUN] ' if not confirm else ''}Order preview:")
    print(f"  ┌─────────────────────────────────────┐")
    print(f"  │ Side:     {side}")
    print(f"  │ Token ID: {token_id[:20]}...")
    print(f"  │ Price:    {price*100:.1f}¢ per share")
    print(f"  │ Shares:   {num_shares:.2f}")
    print(f"  │ Cost:     ${total_cost:.2f} USDC")
    print(f"  └─────────────────────────────────────┘")

    if not confirm:
        print("  Add --confirm to actually place this order.\n")
        return

    print("  Placing order...")
    client = build_client()

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=num_shares,
        side=side,
    )

    signed_order = client.create_order(order_args)
    response = client.post_order(signed_order, OrderType.GTC)

    order_id = response.get("orderID") or response.get("id", "unknown")
    status = response.get("status", "unknown")

    print(f"\n  Order placed!")
    print(f"  Order ID: {order_id}")
    print(f"  Status:   {status}\n")


# ── Live P&L ──────────────────────────────────────────────────────────────────

def cmd_pnl():
    client = build_client()
    our_api_key = os.getenv("POLY_API_KEY", "")
    trades = client.get_trades(TradeParams())

    if not trades:
        print("\n  No trades found.\n")
        return

    def our_side(t):
        """
        The trade's `side` field reflects the TAKER's perspective, not ours.
        When we're the MAKER our real side is in maker_orders.
        When we're the TAKER the trade's side IS our side.
        """
        if t.get("trader_side") == "TAKER":
            return t.get("side", "").upper()
        for order in t.get("maker_orders", []):
            if order.get("owner") == our_api_key:
                return order.get("side", "").upper()
        # Fallback: flip the taker side
        taker = t.get("side", "BUY").upper()
        return "SELL" if taker == "BUY" else "BUY"

    # Aggregate positions by token, sorted chronologically
    positions = {}
    for t in sorted(trades, key=lambda x: int(x.get("match_time", 0))):
        if t.get("status") != "CONFIRMED":
            continue
        asset_id = t["asset_id"]
        size  = float(t["size"])
        price = float(t["price"])
        side  = our_side(t)

        if asset_id not in positions:
            positions[asset_id] = {
                "shares": 0.0, "cost": 0.0,
                "outcome": t.get("outcome", "?"),
                "market": t.get("market", ""),
            }

        if side == "BUY":
            positions[asset_id]["shares"] += size
            positions[asset_id]["cost"]   += size * price
        else:  # SELL = exit
            positions[asset_id]["shares"] -= size
            positions[asset_id]["cost"]   -= size * price

    # Filter out fully closed positions
    open_positions = {k: v for k, v in positions.items() if v["shares"] > 0.01}

    if not open_positions:
        print("\n  No open positions.\n")
        return

    # Fetch current prices from Gamma API
    def get_current_price(asset_id):
        try:
            r = requests.get(f"{GAMMA_API}/markets",
                params={"clob_token_ids": asset_id}, timeout=8)
            r.raise_for_status()
            data = r.json()
            if data:
                prices = data[0].get("outcomePrices", "[]")
                token_ids = data[0].get("clobTokenIds", "[]")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)
                if asset_id in token_ids:
                    idx = token_ids.index(asset_id)
                    return float(prices[idx]), data[0].get("question", "Unknown market")
        except Exception:
            pass
        return None, "Unknown market"

    print(f"\n{'═'*70}")
    print(f"  LIVE P&L  ({len(open_positions)} open position(s))")
    print(f"{'═'*70}\n")

    total_cost = total_value = 0.0

    for asset_id, pos in open_positions.items():
        shares = pos["shares"]
        cost   = pos["cost"]
        avg_price = cost / shares if shares else 0

        current_price, question = get_current_price(asset_id)
        if current_price is None:
            current_price = avg_price  # fallback

        # Skip resolved-to-zero positions (lost/expired)
        if current_price == 0 and cost > 0:
            total_cost  += cost
            total_value += 0
            continue

        value = shares * current_price
        pnl   = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0

        total_cost  += cost
        total_value += value

        print(f"  {question[:55]}")
        print(f"  Outcome: {pos['outcome']}  |  {shares:.2f} shares")
        print(f"  Avg fill: {avg_price*100:.1f}¢  →  Now: {current_price*100:.1f}¢  |  P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
        print()

    total_pnl = total_value - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost else 0
    print(f"{'─'*70}")
    print(f"  Total invested: ${total_cost:.2f}  |  Current value: ${total_value:.2f}  |  P&L: ${total_pnl:+.2f} ({total_pct:+.1f}%)")
    print(f"{'═'*70}\n")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    confirm = "--confirm" in args
    args = [a for a in args if a != "--confirm"]

    cmd = args[0].lower()

    if cmd == "pnl":
        cmd_pnl()

    elif cmd == "balance":
        cmd_balance()

    elif cmd == "search":
        if len(args) < 2:
            print("Usage: trading.py search <keyword>")
            sys.exit(1)
        cmd_search(" ".join(args[1:]))

    elif cmd == "orders":
        cmd_orders()

    elif cmd == "cancel":
        if len(args) < 2:
            print("Usage: trading.py cancel <order_id|all>")
            sys.exit(1)
        cmd_cancel(args[1], confirm)

    elif cmd in ("buy", "sell"):
        if len(args) < 4:
            print(f"Usage: trading.py {cmd} <token_id> <price> <size_usdc>")
            sys.exit(1)
        side = cmd.upper()
        token_id = args[1]
        price = float(args[2])
        size_usdc = float(args[3])
        cmd_order(side, token_id, price, size_usdc, confirm)

    else:
        print(f"Unknown command: {cmd}")
        print("Run: python3 trading.py --help")
        sys.exit(1)


if __name__ == "__main__":
    main()
