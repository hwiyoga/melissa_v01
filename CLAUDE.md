# Melissa — AI Polymarket Trading Bot

## Claude Code Environments

| | **roxanne** | **melissa-server** |
|---|---|---|
| Machine | MacBook (local) | Remote Linux server |
| Role | Development | Production |
| Claude Code | Runs locally | Runs remotely (this machine) |
| Typical work | Write/break/fix code, test ideas | Live trading, running bot in loop mode |
| Code flow | Push changes to GitHub | Pull from GitHub to update codebase |

**Workflow:** develop on roxanne → push to GitHub → pull on melissa-server → run live.

Implication for Claude: on **roxanne**, favor dev-friendly suggestions (testing, iteration, paper mode). On **melissa-server**, be conservative — changes here affect live trading.

Melissa is a semi-automated prediction market trading bot for [Polymarket](https://polymarket.com) (Polygon). It uses Claude to analyze binary options markets, recommend trades with confidence levels, and execute them after human (or auto) approval via Telegram or terminal.

## Architecture

```
Fetch Markets (Gamma API)
  → Format for Claude (price trends, Binance spot data, lessons)
    → Claude recommends trades (JSON: token_id, side, price, confidence, reasoning)
      → Token validation → Human approval (Telegram or terminal)
        → Execute via CLOB API or log to paper_trades.json
```

Key files:
- `bot.py` — main entry point, orchestrates the full trading loop
- `trading.py` — direct CLOB interaction CLI (balance, search, buy/sell)
- `setup_trading.py` — one-time wallet credential derivation
- `paper_pnl.py` — paper trade analytics
- `generate_report.py` — live trading P&L report
- `simulate_strategy.py` — backtester for 15m markets
- `markets.py` — market fetching helper
- `lessons.md` — operational learnings appended to Claude's system prompt
- `paper_trades.json` / `live_trades.json` — trade logs (gitignored)

## Running the Bot

```bash
# Setup (one-time)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 setup_trading.py  # derives Polymarket API credentials

# Single run (paper)
python3 bot.py --paper

# Loop mode (scans every 5 min, 30 min cooldown after no-trade calls)
python3 bot.py --paper --loop

# Auto-execute HIGH confidence trades without approval prompt
python3 bot.py --paper --auto

# Live trading (requires funded Polygon wallet + .env credentials)
python3 bot.py

# Direct trading CLI
python3 trading.py balance
python3 trading.py buy <token_id> <price> <size_usdc> --confirm
```

## Analysis Tools

```bash
python3 paper_pnl.py --detail        # paper trade P&L
python3 simulate_strategy.py         # backtest 15m strategy
python3 generate_report.py           # live trading report
```

## Environment Variables (.env)

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `POLY_PRIVATE_KEY` | Polygon wallet private key |
| `POLY_API_KEY` / `POLY_API_SECRET` / `POLY_API_PASSPHRASE` | Derived by setup_trading.py |
| `POLY_FUNDER` | Wallet address |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional push notifications + approval |

## Key Constants (bot.py)

- `DEFAULT_ORDER_SIZE = 5.0` USDC per trade
- `MAX_ORDER_SIZE = 25.0` hard safety cap
- `COINS = ["btc", "eth", "sol", "xrp"]`
- `DEVIATION_THRESHOLD = 0.08` (8¢ from 50/50 triggers Claude)
- `COOLDOWN_MINUTES = 30` after a no-trade Claude call
- Claude model: `claude-opus-4-6`, max tokens: 2048

## Conventions

- **Market slugs** are time-based: `<coin>-updown-5m-<unix_timestamp>` (300s increments for 5m, 900s for 15m)
- **Token validation**: all Claude recommendations are validated against `market_index` before execution; unrecognized tokens are dropped silently
- **Approval flow**: Telegram-first (5 min timeout), falls back to terminal prompt
- **Claude retries**: exponential backoff on 529 (overloaded) — 15s → 30s → 45s → 60s, up to 4 attempts
- **PID lock** at `/tmp/melissa_bot.pid` prevents duplicate instances
- **lessons.md** is injected into Claude's system prompt — update it when new failure patterns emerge

## Safety Rules

- Never remove the `MAX_ORDER_SIZE` cap or PID lock
- Always validate token IDs against `market_index` before trading
- `trading.py` defaults to dry-run; `--confirm` flag required for live execution
- `.env`, trade logs, and PID files are gitignored — do not commit secrets
