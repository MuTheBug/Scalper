"""Main scalping bot loop.

Orchestrates market data ingestion, signal evaluation, order placement,
and exit management in line with the 9-step strategy defined in
"Comprehensive Quantitative Framework for Dogecoin Scalping" (2026).

Logging is intentionally chatty — every tick emits a one-line status
plus DEBUG-level traces of each confluence gate so you can audit why
a trade was or wasn't taken.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import time
from typing import Optional

from dotenv import load_dotenv

from .config import CONFIG, BotConfig
from .execution.binance_client import BinanceFuturesClient
from .risk.manager import OpenTrade, RiskManager
from .strategy.signals import SignalEngine, compute_psar_trail


logger = logging.getLogger("scalper")


class ScalpingBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        self.client = BinanceFuturesClient(
            api_key=api_key,
            api_secret=api_secret,
            testnet=config.testnet,
            symbol=config.symbol,
        )
        self.signals = SignalEngine(config)
        self.risk = RiskManager(config)
        self.active: Optional[OpenTrade] = None
        self._running = True
        self._tick_count = 0
        self._signal_count = 0

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        logger.info("=" * 80)
        logger.info(
            "Starting DOGE scalper | symbol=%s testnet=%s dry_run=%s leverage=%dx",
            self.config.symbol, self.config.testnet, self.config.dry_run, self.config.leverage,
        )
        logger.info(
            "Strategy: 100-EMA trend on %s, Lorentzian (thr=%+d) on %s, "
            "StochRSI+EWO+volume confluence, 1.5xATR stop, TP1 at 1:1 (%.0f%%), PSAR runner",
            self.config.trend_interval, self.config.lorentzian_threshold,
            self.config.execution_interval, self.config.tp1_fraction * 100,
        )
        logger.info(
            "Risk: base %.2f%% / cap %.2f%% per trade | small_account_mode=%s (auto-widens up to %.0f%%)",
            self.config.risk_per_trade * 100, self.config.max_risk_per_trade * 100,
            self.config.small_account_mode, self.config.small_account_max_risk * 100,
        )
        logger.info("=" * 80)

        # Always try to discover the true exchange filters.
        self._discover_symbol_filters()

        if not self.config.dry_run:
            self.client.set_leverage(self.config.leverage)

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — keep the loop alive
                logger.exception("Tick failed")
            time.sleep(self.config.poll_interval_s)

        logger.info("Bot stopped. Ticks=%d signals=%d", self._tick_count, self._signal_count)

    def _discover_symbol_filters(self) -> None:
        try:
            self.client.load_symbol_filters()
            logger.info(
                "Exchange filters: price_prec=%d qty_prec=%d step_size=%s tick_size=%s min_notional=$%.2f",
                self.client.price_precision, self.client.qty_precision,
                self.client._step_size, self.client._tick_size, self.client.min_notional,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not fetch exchange filters (%s). Falling back to config defaults: "
                "price_prec=%d qty_prec=%d min_notional=$%.2f",
                exc, self.config.price_precision, self.config.quantity_precision,
                self.config.min_notional,
            )

    def _shutdown(self, *_args) -> None:
        logger.info("Shutdown signal received, exiting main loop")
        self._running = False

    # ---- per-tick logic ----------------------------------------------------

    def _tick(self) -> None:
        cfg = self.config
        self._tick_count += 1
        tick_id = self._tick_count

        try:
            execution_df = self.client.klines(cfg.execution_interval, cfg.klines_limit)
            trend_df = self.client.klines(cfg.trend_interval, cfg.klines_limit)
            snapshot = self.client.order_book(cfg.orderbook_depth)
            trades = self.client.recent_trades(limit=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tick %d: market data fetch failed: %s", tick_id, exc)
            return

        current_price = snapshot.mid
        equity = self._get_equity()

        if cfg.verbose_ticks:
            pos_info = "FLAT"
            if self.active:
                plan = self.active.plan
                pos_info = (
                    f"{plan.side.upper()} qty={plan.total_quantity:.4f} "
                    f"entry={plan.entry:.6f} tp1_filled={self.active.tp1_filled}"
                )
            logger.info(
                "Tick %04d | %s=%s=%.6f | spread=%.6f | equity=$%.4f | pos=%s",
                tick_id, cfg.symbol, "mid", current_price, snapshot.spread, equity, pos_info,
            )

        # 1) Manage any open trade before looking for new entries.
        if self.active is not None:
            self._manage_open_trade(execution_df, current_price)
            return

        # 2) Hunt for a fresh signal.
        signal_result = self.signals.evaluate(execution_df, trend_df, snapshot, trades)
        if signal_result is None:
            return

        self._signal_count += 1
        plan = self.risk.build_plan(
            side=signal_result.side,
            entry=signal_result.entry,
            stop=signal_result.stop,
            equity=equity,
            min_notional=self.client.min_notional,
            step_size=self.client._step_size,
        )
        if plan is None:
            logger.info("Signal rejected by risk sizing")
            return

        logger.info(
            "EXECUTE %s %s qty=%.4f entry=%.6f stop=%.6f tp1=%.6f "
            "risk=$%.4f (%.2f%%) notional=$%.4f margin=$%.4f [%s]",
            plan.side.upper(), cfg.symbol, plan.total_quantity, plan.entry,
            plan.stop, plan.tp1, plan.risk_usd, plan.risk_pct_used * 100,
            plan.notional, plan.margin_required, plan.reason,
        )
        if self.config.dry_run:
            logger.info("DRY RUN — no orders sent to exchange")
            self.active = OpenTrade(plan=plan)
            return

        self._open_position(plan)

    # ---- trade lifecycle ---------------------------------------------------

    def _open_position(self, plan) -> None:
        cfg = self.config
        try:
            if cfg.prefer_maker:
                logger.debug(
                    "Placing post-only limit %s qty=%.4f @ %.6f",
                    plan.side, plan.total_quantity, plan.entry,
                )
                self.client.place_limit(plan.side, plan.total_quantity, plan.entry, post_only=True)
                time.sleep(cfg.maker_post_only_timeout_s)
                position = self.client.position()
                filled_qty = abs(float(position.get("positionAmt", 0.0)))
                filled = filled_qty >= plan.total_quantity * 0.5
                logger.debug("Post-only check: filled_qty=%.4f (required %.4f)", filled_qty, plan.total_quantity * 0.5)
                if not filled:
                    logger.info("Post-only entry did not fill — cancelling and crossing the spread")
                    self.client.cancel_all()
                    self.client.place_market(plan.side, plan.total_quantity)
            else:
                logger.debug("Placing market %s qty=%.4f", plan.side, plan.total_quantity)
                self.client.place_market(plan.side, plan.total_quantity)

            logger.debug("Placing stop-market close @ %.6f", plan.stop)
            self.client.place_stop_market(plan.side, plan.stop, close_position=True)
            logger.debug("Placing TP1 @ %.6f qty=%.4f", plan.tp1, plan.tp1_quantity)
            self.client.place_take_profit(plan.side, plan.tp1, plan.tp1_quantity)
            self.active = OpenTrade(plan=plan)
            logger.info("Position opened and brackets armed")
        except Exception:
            logger.exception("Order submission failed — flattening")
            self.client.cancel_all()

    def _manage_open_trade(self, execution_df, current_price: float) -> None:
        assert self.active is not None
        trade = self.active
        plan = trade.plan
        stop = trade.trailing_stop if trade.trailing_stop is not None else plan.stop

        logger.debug(
            "Managing %s trade | price=%.6f entry=%.6f stop=%.6f tp1=%.6f tp1_filled=%s",
            plan.side, current_price, plan.entry, stop, plan.tp1, trade.tp1_filled,
        )

        if self.risk.stop_hit(trade, current_price):
            logger.info("STOP-LOSS hit at %.6f (stop=%.6f)", current_price, stop)
            self._close_trade("stop")
            return

        if not trade.tp1_filled and self.risk.tp1_hit(trade, current_price):
            logger.info("TP1 hit at %.6f — scaling out %.0f%% and advancing stop to break-even",
                        current_price, self.config.tp1_fraction * 100)
            self.risk.advance_to_break_even(trade)
            if not self.config.dry_run:
                self.client.cancel_all()
                self.client.place_stop_market(plan.side, plan.entry, close_position=True)
            return

        # Runner management: trail with Parabolic SAR
        if trade.tp1_filled:
            psar = compute_psar_trail(execution_df, self.config.psar_step, self.config.psar_max)
            logger.debug("PSAR trail candidate: %.6f (current stop %.6f)", psar, trade.trailing_stop or 0.0)
            if self.risk.update_trailing_stop(trade, psar):
                if not self.config.dry_run:
                    self.client.cancel_all()
                    self.client.place_stop_market(
                        plan.side, trade.trailing_stop, close_position=True,
                    )

    def _close_trade(self, reason: str) -> None:
        assert self.active is not None
        trade = self.active
        if not self.config.dry_run:
            remaining = (
                trade.plan.runner_quantity if trade.tp1_filled else trade.plan.total_quantity
            )
            try:
                self.client.place_market(
                    "short" if trade.plan.side == "long" else "long",
                    remaining,
                    reduce_only=True,
                )
            finally:
                self.client.cancel_all()
        logger.info("Trade closed | reason=%s side=%s entry=%.6f",
                    reason, trade.plan.side, trade.plan.entry)
        self.active = None

    # ---- helpers -----------------------------------------------------------

    def _get_equity(self) -> float:
        if self.config.dry_run:
            return float(os.environ.get("DRY_RUN_EQUITY", "1000.0"))
        try:
            return self.client.available_balance(self.config.quote_asset)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch balance: %s", exc)
            return 0.0


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(cfg: BotConfig) -> None:
    level = os.environ.get("LOG_LEVEL", cfg.log_level).upper()
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_file = os.environ.get("LOG_FILE", cfg.log_file)
    if log_file:
        try:
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not open log file {log_file}: {exc}", file=sys.stderr)

    # Silence overly-noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def main() -> None:
    load_dotenv()
    CONFIG.testnet = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
    CONFIG.dry_run = os.environ.get("DRY_RUN", "true").lower() == "true"
    _setup_logging(CONFIG)

    bot = ScalpingBot(CONFIG)
    try:
        bot.start()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
