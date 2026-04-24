# DOGE Scalping Bot (Binance Futures)

An implementation of the 9-step scalping strategy described in
`Dogecoin Scalping Strategy Steps.pdf`, targeted at Binance USD-M Futures.

The bot is **defensive by default**: it runs against the Binance testnet and
in dry-run mode unless you explicitly opt in to live trading.

## Honest profitability expectations

No scalping strategy wins 100% of the time. The PDF itself targets a
**70-80% win rate**, not 100%. Anything that claims to produce only
winning trades is either peeking at future data (look-ahead bias) or
overfitting a single historical sample so tightly that it will blow
up on live data. This bot does neither.

Crypto 1-minute scalping on REST-kline backtests typically produces
win rates in the 35-50% range. That can still be profitable if R:R
> 1, if the loss-streak cooldown holds drawdowns in check, and if the
trend-strength filters keep you out of chop. But it is **not** the
PDF's 70-80% — that figure depends on real-time DOM and absorption
reads that a historical-kline backtest can't replicate.

What you control:

| Profile      | Signal frequency | R:R at TP1 | Filters | Intent |
|--------------|------------------|------------|---------|--------|
| `strict`     | Very low         | 1.0R       | ADX≥22, primary-confluence-only | Faithful to the PDF. |
| `quality`    | Low              | 1.5R       | ADX≥22, EWO mag ≥0.10, tight pullback/breakout | Fewer, higher-conviction trades. Loses money on random/sideways data, wins when markets actually trend. |
| `balanced`   | Moderate         | 1.2R       | ADX≥18 | Middle ground; default if unsure. |
| `aggressive` | High             | 1.0R       | ADX≥12 | Many trades, lots of noise. Highest drawdown risk. |

All profiles share:

- Loss-streak cooldown: pauses trading for 30-40 bars after 2 consecutive stops.
- Daily loss cap: halts trading for 24h when cumulative R < -5.
- 1.5× ATR stops; position sized to 1% equity (auto-widens to meet min-notional
  on small accounts).

Backtest, tune, and validate on testnet before risking real money.

## Strategy profiles

Select at runtime:

```bash
python -m scalper.backtest --profile strict     --bars 3000
python -m scalper.backtest --profile quality    --bars 3000
python -m scalper.backtest --profile balanced   --bars 3000
python -m scalper.backtest --profile aggressive --bars 3000
```

Each profile tweaks:

- `lorentzian_threshold` (5 → 3 → 3 → 2)
- `lorentzian_lookback` (2000 → 500 → 500 → 300)
- `stoch_rsi_oversold/overbought` (30/70 → 32/68 → 35/65 → 45/55)
- `stoch_cross_lookback` (1 → 3 → 5 → 10 bars)
- `volume_confirmation_multiplier` (1.2× → 1.2× → 1.0× → 0.8×)
- `tp1_rr` and `tp1_fraction` (the runner vs. scalp-out mix)
- `min_adx` (trend-strength floor)
- `enable_pullback_entries` / `enable_breakout_entries` (off → on → on → on)

See `scalper/config.py::PROFILES` for full definitions. Every profile
enforces:

- **Pullback quality**: real dip into the trend EMA within the last
  5-6 bars, RSI in the pullback zone (35-55 for long, 45-65 for short),
  volume expansion on the bounce bar.
- **Breakout quality**: clean breach of 20-bar high/low, ATR > 1.2-1.3×
  its moving average, body ≥ 55-60% of bar range, directional close.
- **Trend strength**: ADX ≥ profile's `min_adx`.

Your earlier -10R result with 37% win rate was the previous (looser)
pullback logic firing on chop. The current pipeline trades far less
often but with meaningfully higher setup quality; expect your next
backtest to show fewer trades, higher average R, and much lower
drawdown — but still not 100%.

## Strategy overview

Each tick the bot walks the PDF's confluence cascade before taking a trade:

| Step | Module | What it does |
|------|--------|--------------|
| 1    | `execution/binance_client.py`        | Binance Futures REST client, price/qty rounding, maker/taker routing |
| 2    | (external)                           | Sentiment webhooks (IFTTT/Zapier/3Commas) — connect via custom signal feed |
| 3    | `strategy/signals.py::trend_bias`    | 100-period EMA on the 15m chart decides long-only vs short-only |
| 4    | `strategy/order_flow.py`             | Wall detection + absorption / delta analysis on the order book |
| 5    | `strategy/indicators.py::lorentzian_classify` | k-NN with Lorentzian distance over RSI/CCI/ADX/volume features |
| 6    | `strategy/signals.py::confluence`    | Stoch RSI cross + EWO sign + volume > 20-MA confirmation |
| 7    | `risk/manager.py`                    | 1.5× ATR stop, per-trade risk capped at 1% (max 2%) of equity |
| 8    | `bot.py::_open_position`             | Maker-first entry with market-order fallback, TP1 limit at 1:1 for 70% of size |
| 9    | `bot.py::_manage_open_trade`         | Break-even stop after TP1, Parabolic SAR trail for the runner |

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your Binance testnet keys
```

Create keys on the [Binance Futures testnet](https://testnet.binancefuture.com/)
and paste them into `.env`.

## Run

**Dry-run (default — signals only, no orders sent):**

```bash
python -m scalper
```

**Backtest on recent klines:**

```bash
python -m scalper.backtest --bars 1500
```

**Live trading on testnet** (`DRY_RUN=false`, `BINANCE_TESTNET=true` in `.env`):

```bash
DRY_RUN=false python -m scalper
```

**Mainnet** — only after extensive testnet validation:

```bash
BINANCE_TESTNET=false DRY_RUN=false python -m scalper
```

## Configuration

All tunables live in `scalper/config.py`:

- `lorentzian_threshold` — PDF recommends +4 to +6 for DOGE; default 5
- `atr_stop_multiplier` — 1.5 per the PDF
- `risk_per_trade` / `max_risk_per_trade` — 1% / 2% equity cap
- `tp1_rr` / `tp1_fraction` — 1.0 R / 70% of position liquidated at TP1
- `prefer_maker` — set `False` to route taker-only (simpler but higher fees)
- `leverage` — default 10x; the PDF does not prescribe a level, tune carefully
- `small_account_mode` — when `True` (default), the risk manager auto-widens
  risk just enough to meet the exchange's MIN_NOTIONAL filter. See below.
- `small_account_max_risk` — hard cap on the widened risk; default 25%
- `absolute_min_capital` — bot refuses to trade below this; default $0.50

### Small-account mode (trading with < $3)

Binance Futures enforces a ~$5 MIN_NOTIONAL filter on DOGEUSDT. A literal
1% risk on $3 of equity produces a $3 position, which the exchange will
reject. When `small_account_mode=True` the bot:

1. Computes the smallest qty that clears `min_notional` (pulled live from
   `futures_exchange_info`).
2. Back-solves the implied risk percentage.
3. Executes if that risk is ≤ `small_account_max_risk`; otherwise it
   logs a warning and skips the trade.

With `$3` equity, a 0.1% stop distance and the default $5 min-notional,
this works out to ≈1.67% risk ($0.05 at-risk). Check the log line:

```
Small-account mode: widening risk to 1.67% (risk_usd=$0.0500) to satisfy min_notional
```

Leverage (default 10x) is independent: it just determines how much
margin the position locks up. $5 notional at 10x = $0.50 margin.

### Environment overrides

| Variable | Effect |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Exchange credentials |
| `BINANCE_TESTNET` | `true` (default) uses testnet, `false` is mainnet |
| `DRY_RUN` | `true` (default) skips order placement |
| `DRY_RUN_EQUITY` | Equity to assume when dry-running (default 1000) |
| `LOG_LEVEL` | `DEBUG` (default), `INFO`, `WARNING` |
| `LOG_FILE` | Rotating log path (default `scalper.log`) |

### Logs

DEBUG is the default. A typical tick produces:

```
Tick 0012 | DOGEUSDT=mid=0.097321 | spread=0.000010 | equity=$3.00 | pos=FLAT
Step 3 — Trend bias: close=0.097410 ema100=0.095220 -> LONG_ONLY
Step 5 — Lorentzian: signal=+1 (neighbors=8 threshold=5)
Step 6 — Confluence probe (side=long): stoch K=34.12 (prev 24.88) D=28.55 EWO=0.0412 ...
  long gate: stoch_cross_up=True ewo>0=True vol_ok=True -> PASS
Step 4 — Order flow: spread=0.000010 mid=0.097321 walls=2 support=0.097100 resistance=0.098500 absorption=bullish
SIGNAL LONG entry=0.097330 stop=0.097180 ATR=0.000100 ...
Plan built: side=long qty=50.0000 ... risk=$0.0500 (1.67%) notional=$5.0000 ...
EXECUTE LONG DOGEUSDT qty=50.0000 ...
```

A rotating file handler writes the same content to `scalper.log`
(5×5MB by default) so you can post-mortem sessions.

## Project layout

```
scalper/
├── bot.py                  # main loop (entry point: `python -m scalper`)
├── backtest.py             # historical replay + R-multiple report
├── config.py               # BotConfig dataclass
├── execution/
│   └── binance_client.py   # Binance Futures REST wrapper
├── risk/
│   └── manager.py          # sizing, bifurcated TP/SL, trailing stops
└── strategy/
    ├── indicators.py       # EMA/RSI/StochRSI/EWO/ATR/CCI/ADX/PSAR/Lorentzian
    ├── order_flow.py       # DOM walls + absorption detection
    └── signals.py          # Confluence engine
```

## Caveats

- The PDF is a framework, not a guarantee. The strategy's 70-80% win-rate
  claim presupposes meticulous parameter tuning to the current regime and
  honest order-book tape reading; expect to iterate.
- Sentiment ingestion (PDF Step 2) is intentionally left to an external
  webhook (3Commas / custom IFTTT pipeline). The bot exposes no inbound
  webhook server by design — trade triggers should come from the confluence
  engine, not from raw tweets, unless you have latency infrastructure that
  can beat the HFT cohort.
- The Lorentzian classifier is a retail-grade approximation of the
  TradingView Pine implementation. It uses forward-looking labels in
  training, so the backtest harness restricts lookup to bars strictly
  before the current index. If you port it to live, double-check.
- Always run on testnet until you have a stable equity curve.

## Risk disclosure

Scalping futures with leverage is a losing game for most retail
participants. This code is provided as an educational reference, not
financial advice. Trade with capital you can afford to lose.
