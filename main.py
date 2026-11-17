"""cbBTC mean-reversion bot for make.fun btc-race-24h-20261117-0603.

Protocol: agent-fun/bars-replay/v1 (NDJSON on stdin/stdout).
Pure standard library, deterministic: integer arithmetic only, no clock,
no network, no files, no randomness. One symbol (CBBTC/USDC), long only,
at most one open position, full-size entries (100% of cash).

Strategy (decided on bar t's close, filled at bar t+1's open):
  * Entry: the close has fallen at least 80 bps below the close 240 bars
    (4 hours) earlier -- a dip -- and the dip signal is "armed".
  * Exit: the position is worth 1.00% over its cost (take profit), or
    1.50% under its cost (hard stop), or it has been held 480 bars
    (8 hours, max hold) without hitting either. A trailing exit also
    arms once the position is 0.60% over cost and sells 0.50% below
    its peak value, so a reversion that stalls short of +1.00% and
    then fades is still banked instead of ridden back down.
  * Re-arm latch: after any sell, a new dip only counts once the
    240-bar return has recovered above -30 bps. Without this the bot
    would re-buy the same unresolved dip bar after bar and churn
    away its edge in round-trip costs (~34 bps per round trip).

Why this shape: on the 1,440-bar dev window the price is a choppy,
roughly flat random walk (+0.49% start to end) in which trend
following loses on whipsaw (the house EMA-cross baseline: -$106 on
68 trades) while 0.8-2% dips reliably revert within hours. Every
trade therefore buys a verified dip and sells a verified recovery;
the bot is flat most of the time, so a flat or falling market
costs it nothing but the round trips it actually takes.
"""

import json
import sys

PROTOCOL = "agent-fun/bars-replay/v1"

# Strategy parameters (bars are 1 minute; bps are basis points).
LOOKBACK = 240          # bars for the dip measurement (4 hours)
DROP_BPS = 80           # entry: 240-bar return <= -80 bps
REARM_BPS = -30         # re-arm latch: 240-bar return > -30 bps
TAKE_BPS = 100          # exit: position value >= cost + 1.00%
STOP_BPS = 150          # exit: position value <= cost - 1.50%
TRAIL_ARM_BPS = 60      # trailing exit arms once value >= cost + 0.60%
TRAIL_BPS = 50          # ... then sells 0.50% below the position's peak value
MAX_HOLD = 480          # exit: held this many bars (8 hours)
BUY_SLIPPAGE_BPS = 150  # don't chase a buy more than 1.5% past the close
SELL_SLIPPAGE_BPS = 300  # let a sell out run even in a fast downdraft

SYMBOL = "CBBTC"


def value_of(qty, price, qty_scale, price_scale, quote_scale):
    """What qty is worth at price, in quote units (truncating, as the harness does)."""
    return qty * price * 10 ** quote_scale // 10 ** (qty_scale + price_scale)


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.seed = hello["seed"]
        self.symbols = {s["symbol"]: s for s in hello["symbols"]}
        self.capital_units = int(hello["capital"]["quote_units"])
        self.quote_scale = int(hello["capital"]["quote_scale"])
        self.caps = hello["caps"]
        self.fees = hello["fees"]
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.cash = self.capital_units
        self.equity = self.capital_units
        self.positions = {}  # symbol -> (qty, cost) as ints
        self.closes = []     # CBBTC closes, ints at price_scale, oldest first
        self.latest_close = None
        self.seq = -1
        self.next_id = 1
        # Strategy state.
        self.armed = True
        self.peak_value = None
        self.bars_held = 0

    def apply_batch(self, batch):
        self.seq = batch["seq"]
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self.closes.append(int(bar["c"]))
                self.latest_close = int(bar["c"])
        account = batch["account"]
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {
            p["symbol"]: (int(p["qty"]), int(p["cost_quote_units"]))
            for p in account["positions"]
        }

    def ret_lookback_bps_cross(self, threshold_bps):
        """Compare the LOOKBACK-bar return with threshold_bps without floats.

        Returns +1 if return > threshold, -1 if < threshold, 0 if equal,
        None before LOOKBACK bars of history exist.
        """
        if len(self.closes) <= LOOKBACK:
            return None
        now = self.closes[-1]
        old = self.closes[-1 - LOOKBACK]
        diff = now - old
        # return <= threshold  <=>  diff * 10000 <= threshold * old
        lhs = diff * 10000
        rhs = threshold_bps * old
        if lhs > rhs:
            return 1
        if lhs < rhs:
            return -1
        return 0

    def decide(self):
        """Orders for this batch (empty list for none)."""
        if self.seq < self.warmup_bars:
            return []
        if self.latest_close is None:
            return []
        symbol = self.symbols.get(SYMBOL)
        if symbol is None:
            return []
        qty_scale = int(symbol["qty_scale"])
        price_scale = int(symbol["price_scale"])
        held = self.positions.get(SYMBOL)
        min_notional = int(self.fees["min_notional_quote_units"])

        if held is not None:
            qty, cost = held
            val = value_of(qty, self.latest_close, qty_scale,
                           price_scale, self.quote_scale)
            if self.peak_value is None or val > self.peak_value:
                self.peak_value = val
            self.bars_held += 1
            sell = False
            if val * 10000 >= cost * (10000 + TAKE_BPS):
                sell = True  # take profit: +1.00% over cost
            elif val * 10000 <= cost * (10000 - STOP_BPS):
                sell = True  # hard stop: -1.50% under cost
            elif val * 10000 >= cost * (10000 + TRAIL_ARM_BPS) \
                    and val * 10000 <= self.peak_value * (10000 - TRAIL_BPS):
                sell = True  # trailing: +0.60% armed, then -0.50% off peak
            elif self.bars_held >= MAX_HOLD:
                sell = True  # max hold: 8 hours without a resolution
            if sell:
                self.armed = False
                self.peak_value = None
                self.bars_held = 0
                return [{
                    "side": "sell",
                    "client_id": self._client_id(),
                    "symbol": SYMBOL,
                    "qty": str(qty),
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                }]
            return []

        # Flat: maintain the re-arm latch, then look for a dip.
        self.peak_value = None
        self.bars_held = 0
        cmp = self.ret_lookback_bps_cross(REARM_BPS)
        if cmp is None:
            return []
        if cmp > 0:
            self.armed = True
        if not self.armed:
            return []
        if self.ret_lookback_bps_cross(-DROP_BPS) == -1:
            room = int(self.caps["max_weight_bps"]) * self.equity // 10000
            notional = self.cash if self.cash < room else room
            if notional < min_notional:
                return []
            return [{
                "side": "buy",
                "client_id": self._client_id(),
                "symbol": SYMBOL,
                "notional_quote_units": str(notional),
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            }]
        return []

    def _client_id(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid


def main():
    bot = None
    out = sys.stdout
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                raise SystemExit("unsupported protocol: %r" % (msg.get("protocol"),))
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                raise SystemExit("batch before hello")
            bot.apply_batch(msg)
            orders = bot.decide()
            out.write(json.dumps(
                {"type": "reply", "seq": msg["seq"], "orders": orders},
                separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return 0
        else:
            raise SystemExit("unknown message type: %r" % (kind,))
    raise SystemExit("input ended before done")


if __name__ == "__main__":
    sys.exit(main())
