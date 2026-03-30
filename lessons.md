# Melissa — Lessons from Failed Trades

These lessons are derived from real trade outcomes. Apply them when evaluating new signals.

---

## LESSON 1: Crypto 5-min/15-min Up/Down Markets — Use the Price Trend
**Pattern:** Melissa repeatedly flagged crypto up/down windows as HIGH confidence when prices deviated from 50/50 (e.g. Down at 70¢, Up at 30¢). These almost always lost.
**Why:** The deviation reflects real momentum. Active traders in these windows are already positioned based on live price movement. A market at 70¢ Down almost certainly means crypto IS falling at that moment. Fading momentum in a 5-15 minute window is not an edge.
**New data available:** Each outcome now includes a "Trend" line showing recent price movement, e.g. "RISING ↑ 43¢ → 47¢ → 52¢ (+9¢ in ~20 min)".
**Rule:**
- If trend direction MATCHES the leading side (e.g. Down is winning AND trend is FALLING) → momentum is confirmed, do NOT fade it. Skip or confidence LOW only.
- If trend is FLAT → deviation may be stale/noise, mean reversion possible. Medium confidence acceptable.
- If trend CONTRADICTS the leading side (e.g. Down is winning but trend is RISING) → potential reversal signal. This is the one case where fading has merit. Medium confidence max.
- Never assign HIGH confidence to crypto up/down windows based on deviation alone.

---

## LESSON 2: Sports Spread and News Markets — Real Edge Exists
**Pattern:** Bets on sports spreads (Celtics -6.5) and news markets (DHS shutdown) were profitable.
**Why:** These markets have longer resolution windows, and Claude's knowledge of base rates, team form, and news context provides genuine information advantage over the market price.
**Rule:** Prioritize today's-market category signals over crypto up/down. Sports and news bets have demonstrated real edge. Be more willing to flag these as HIGH confidence when the mispricing is clear.

---

## LESSON 3: Averaging Down on Losing Positions
**Pattern:** The Indonesia trade deal (US/Indonesia) was bought twice ($4 + $6 = $10 total), then sold for $3.52 — a $6.48 loss. The second buy was averaging down on a position that was already moving against us.
**Rule:** Never recommend buying more of a market where a position already exists at a worse price. If the market has moved against a prior bet, that is evidence the original thesis was wrong.

---

## LESSON 4: Long-Tail Geopolitical Bets at Extreme Odds
**Pattern:** France/UK/Germany strike Iran (bought Yes at ~7¢, expired at 0). Fed 50bps cut (bought Yes at 0.4¢, expired at 0). Both were low-probability bets that lost.
**Rule:** Avoid recommending bets on extreme long-shots (<10¢) for geopolitical events unless there is a very specific, time-sensitive catalyst. These are lottery tickets, not edges.

---

## LESSON 5: Duplicate Melissa Instances — API Cost Explosion
**Pattern:** On 2026-03-30, 7 Melissa instances ran simultaneously (live + paper). Each called Claude every 5 minutes at ~$0.08/call. This burned through 70% of the Anthropic API budget in under a day (~$6.72/hour at peak).
**Why:** Each `pkill` or restart attempt left old processes running. Claude Code session restarts also left orphaned background processes.
**Fix applied:** PID lock file at `/tmp/melissa_bot.pid`. A new instance checks if the PID is alive and refuses to start if so. Lock is released on clean exit.
**Rule:** Never start Melissa without first running `ps aux | grep bot.py` to confirm no existing instance. If in doubt, `pkill -f bot.py` before starting.

---

*Last updated: 2026-03-30. Add new lessons as patterns emerge.*
