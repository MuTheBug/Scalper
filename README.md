# DOGE Scalping Bot (Binance Futures)

An implementation of the 9-step scalping strategy described in
`Dogecoin Scalping Strategy Steps.pdf`, targeted at Binance USD-M Futures.

The bot is **defensive by default**: it runs against the Binance testnet and
in dry-run mode unless you explicitly opt in to live trading.

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
- `leverage` — default 5x; the PDF does not prescribe a level, tune carefully

Environment overrides:

| Variable | Effect |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Exchange credentials |
| `BINANCE_TESTNET` | `true` (default) uses testnet, `false` is mainnet |
| `DRY_RUN` | `true` (default) skips order placement |
| `LOG_LEVEL` | e.g. `DEBUG`, `INFO` |

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
