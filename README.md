# cbbtc-race-bot

A mean-reversion trading bot for the make.fun competition
**"Race a BTC trading bot live for 24 hours"**
(`btc-race-24h-20261117-0603`, $250 prize pool).

It trades Coinbase Wrapped BTC (cbBTC) against USDC on 1-minute bars,
starting from $1,000 of paper money, long only, one position at a time.

## Strategy

Buy the dip, sell the recovery — nothing else.

- **Entry:** the close has fallen at least **0.80%** below the close
  240 bars (4 hours) earlier, and the dip signal is *armed*.
  Full size: 100% of cash.
- **Exit (first hit wins):**
  - take profit at **+1.00%** of position value over cost;
  - hard stop at **−1.50%** under cost;
  - trailing exit: arms at **+0.60%** over cost, sells **0.50%**
    below the position's peak value;
  - max hold **480 bars** (8 hours) without a resolution.
- **Re-arm latch:** after any sell, a new dip only counts once the
  240-bar return has recovered above **−0.30%**. Without this the bot
  would re-buy the same unresolved dip bar after bar and churn its
  edge away in round-trip costs (~34 bps per round trip: 4 bps fee,
  2 bps slippage, ~13 bps price impact each way at this pool depth).

Why this shape: on the 1,440-bar dev window the price is a choppy,
roughly flat random walk (+0.49% start to end) in which trend
following loses on whipsaw (the house EMA-cross baseline scores
−$106 on 68 trades) while 0.8–2% dips reliably revert within hours.
The bot is flat most of the time, so a flat or falling market costs
it nothing but the round trips it actually takes.

## Dev-window result (local replay, faithful harness)

| Metric | Value |
|---|---|
| Net PnL | **+$26.20 USDC** |
| Trades | 7 (4 buys, 3 sells; final position flattened) |
| Max drawdown | 1.05% |
| Fees paid | $3.25 |
| Rejected orders | 0 |
| Determinism | two runs, replies byte-for-byte identical |

For comparison on the same window: buy-and-hold +$1.07,
momentum baseline +$2.45, EMA-cross −$106.21, RSI-Bollinger −$24.25.

## How to run / grade

The bot is a single self-contained file, Python 3.13, standard
library only — no installs, no network, no wall clock, no files
outside the repository, no randomness. It speaks
`agent-fun/bars-replay/v1` (NDJSON on stdin/stdout).

```sh
# with the make.fun kit (grade-local needs Docker for Python bots;
# on a machine without it, use a faithful local replay instead)
node cli/makefun.mjs grade-local --kit <kit-dir> --bot . --driver docker

# smoke test: feed it the protocol by hand
python3 -P -s main.py
```

## Files

- `main.py` — the bot (entrypoint, repository root).
- `LICENSE` — MIT.
- `.makefun/owner.json` — claim file for the entry wallet.
