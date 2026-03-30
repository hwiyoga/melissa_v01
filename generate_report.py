"""
Generate a P&L report using the Polymarket Data API (no CSV export needed).
Also reads live_trades.json for Melissa's session reasoning/notes.

Usage:
  python3 generate_report.py                    — print to stdout
  python3 generate_report.py --out /path/to.txt — write to file
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_API         = "https://data-api.polymarket.com"
LIVE_TRADES_FILE = "live_trades.json"
WALLET           = os.getenv("POLY_FUNDER", "")


def fetch_positions() -> list[dict]:
    try:
        r = requests.get(f"{DATA_API}/positions", params={"user": WALLET, "limit": 100}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def fetch_activity(limit: int = 200) -> list[dict]:
    try:
        r = requests.get(f"{DATA_API}/activity", params={"user": WALLET, "limit": limit}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def compute_pnl(activity: list[dict]) -> dict:
    """Compute P&L totals from raw activity (TRADE + REDEEM events)."""
    bought = sold = redeemed = deposits = withdrawals = 0.0
    wins = losses = 0

    for a in activity:
        atype = a.get("type", "")
        usdc  = float(a.get("usdcSize", 0))
        side  = a.get("side", "")

        if atype == "TRADE":
            if side == "BUY":
                bought += usdc
            elif side == "SELL":
                sold += usdc
        elif atype == "REDEEM":
            redeemed += usdc
            if usdc > 0:
                wins += 1
            else:
                losses += 1
        elif atype == "DEPOSIT":
            deposits += usdc
        elif atype == "WITHDRAW":
            withdrawals += usdc

    net_pnl = (sold + redeemed) - bought
    return {
        "deposits": deposits,
        "withdrawals": withdrawals,
        "net_capital_in": deposits - withdrawals,
        "total_bought": bought,
        "total_sold": sold,
        "total_redeemed": redeemed,
        "wins": wins,
        "losses": losses,
        "net_pnl": net_pnl,
    }


def build_report() -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    lines.append("=" * 70)
    lines.append("  MELISSA — FULL P&L REPORT")
    lines.append(f"  Generated: {now_str}")
    lines.append(f"  Wallet: {WALLET}")
    lines.append("=" * 70)

    # ── Positions ────────────────────────────────────────────────────────────
    positions = fetch_positions()
    lines.append("\n── OPEN POSITIONS ──────────────────────────────────────────────────")

    if not positions:
        lines.append("  No open positions.")
    else:
        total_current = 0.0
        total_initial = 0.0
        for p in positions:
            title        = p.get("title", "N/A")
            outcome      = p.get("outcome", "N/A")
            cur_price    = float(p.get("curPrice", 0))
            avg_price    = float(p.get("avgPrice", 0))
            size         = float(p.get("size", 0))
            initial_val  = float(p.get("initialValue", 0))
            current_val  = float(p.get("currentValue", 0))
            cash_pnl     = float(p.get("cashPnl", 0))
            pct_pnl      = float(p.get("percentPnl", 0))
            end_date     = p.get("endDate", "N/A")
            redeemable   = p.get("redeemable", False)

            total_current += current_val
            total_initial += initial_val

            status = "REDEEMABLE" if redeemable else "OPEN"
            lines.append(
                f"\n  [{status}] {title}"
                f"\n    Outcome: {outcome}  |  {size:.2f} shares @ avg {avg_price*100:.1f}¢  →  now {cur_price*100:.1f}¢"
                f"\n    Value: ${initial_val:.2f} → ${current_val:.2f}   P&L: ${cash_pnl:+.2f} ({pct_pnl:+.1f}%)   Ends: {end_date}"
            )

        total_pnl = total_current - total_initial
        lines.append(f"\n  Total open value:  ${total_current:.2f}  (invested ${total_initial:.2f},  P&L ${total_pnl:+.2f})")

    # ── Activity history ─────────────────────────────────────────────────────
    activity = fetch_activity(limit=200)
    lines.append("\n── TRADE HISTORY ───────────────────────────────────────────────────")

    if not activity:
        lines.append("  No activity found.")
    else:
        total_bought   = 0.0
        total_returned = 0.0
        wins = losses  = 0

        for a in activity:
            atype   = a.get("type", "")
            usdc    = float(a.get("usdcSize", 0))
            ts      = int(a.get("timestamp", 0))
            dt_str  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "N/A"
            title   = a.get("title", "N/A")
            outcome = a.get("outcome", "")
            side    = a.get("side", "")
            price   = float(a.get("price", 0))

            if atype == "TRADE":
                price_str = f"@ {price*100:.1f}¢" if price else ""
                lines.append(f"  {dt_str}  {side:<5} ${usdc:<8.2f} {price_str:<10}  {title}  [{outcome}]")

            elif atype == "REDEEM":
                tag = "WIN " if usdc > 0 else "LOSS"
                amt = f"${usdc:.2f}" if usdc > 0 else "$0.00"
                lines.append(f"  {dt_str}  {tag}  {amt:<9}            {title}")

            elif atype in ("DEPOSIT", "WITHDRAW"):
                lines.append(f"  {dt_str}  {atype:<8} ${usdc:.2f}   —")

        pnl = compute_pnl(activity)
        lines.append(f"\n  Total bought:    ${pnl['total_bought']:.2f}")
        lines.append(f"  Total sold:      ${pnl['total_sold']:.2f}")
        lines.append(f"  Total redeemed:  ${pnl['total_redeemed']:.2f}")
        lines.append(f"  Wins / Losses:   {pnl['wins']} / {pnl['losses']}")
        lines.append(f"  Net trading P&L: ${pnl['net_pnl']:+.2f} USDC")

    # ── Melissa session notes ────────────────────────────────────────────────
    if os.path.exists(LIVE_TRADES_FILE):
        with open(LIVE_TRADES_FILE) as f:
            session_trades = json.load(f)

        if session_trades:
            lines.append("\n── MELISSA SESSION NOTES ───────────────────────────────────────────")
            for t in session_trades:
                ts       = t.get("timestamp", "")[:16].replace("T", " ")
                question = t.get("market_question", "N/A")
                outcome  = t.get("outcome", "N/A")
                side     = t.get("side", "BUY")
                price    = float(t.get("limit_price", 0))
                conf     = t.get("confidence", "?").upper()
                reason   = t.get("reasoning", "N/A")
                lines.append(f"\n  {ts}  {side} {outcome} @ {price*100:.1f}¢  [{conf}]")
                lines.append(f"    Market: {question}")
                lines.append(f"    Reason: {reason}")

    # ── Overall summary ──────────────────────────────────────────────────────
    pnl = compute_pnl(activity)
    open_pnl = sum(float(p.get("cashPnl", 0)) for p in positions)

    lines.append("\n" + "=" * 70)
    lines.append("  OVERALL SUMMARY")
    lines.append("=" * 70)
    lines.append(f"  Deposits:          ${pnl['deposits']:.2f} USDC")
    lines.append(f"  Withdrawals:       ${pnl['withdrawals']:.2f} USDC")
    lines.append(f"  Net capital in:    ${pnl['net_capital_in']:.2f} USDC")
    lines.append(f"  ───────────────────────────────")
    lines.append(f"  Total bought:      ${pnl['total_bought']:.2f} USDC")
    lines.append(f"  Total sold:        ${pnl['total_sold']:.2f} USDC")
    lines.append(f"  Total redeemed:    ${pnl['total_redeemed']:.2f} USDC")
    lines.append(f"  Wins / Losses:     {pnl['wins']} / {pnl['losses']}")
    lines.append(f"  ───────────────────────────────")
    lines.append(f"  Closed P&L:        ${pnl['net_pnl']:+.2f} USDC")
    lines.append(f"  Open positions P&L:${open_pnl:+.2f} USDC")
    lines.append(f"  Total P&L:         ${pnl['net_pnl'] + open_pnl:+.2f} USDC")
    lines.append("=" * 70)

    return "\n".join(lines)


if __name__ == "__main__":
    report = build_report()

    out_path = None
    if "--out" in sys.argv:
        idx = sys.argv.index("--out")
        if idx + 1 < len(sys.argv):
            out_path = sys.argv[idx + 1]

    if out_path:
        with open(out_path, "w") as f:
            f.write(report)
        print(f"Report saved to {out_path}")
    else:
        print(report)
