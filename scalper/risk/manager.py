"""Risk & position-sizing (PDF Steps 7-9).

Encapsulates:
  * Per-trade 1-2% equity risk cap (Step 7) with a "small account"
    escape hatch that widens risk just enough to clear Binance's
    MIN_NOTIONAL filter when the wallet is tiny.
  * Bifurcated exit: TP1 at 1:1 for 60-75% of size, TP2 runner trailed
    by Parabolic SAR (Steps 8-9)
  * Break-even advancement after TP1 fills (Step 9)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..config import BotConfig


logger = logging.getLogger(__name__)


@dataclass
class TradePlan:
    side: str
    entry: float
    stop: float
    tp1: float
    tp1_quantity: float
    runner_quantity: float
    total_quantity: float
    risk_usd: float
    notional: float
    risk_pct_used: float
    margin_required: float
    reason: str = ""


@dataclass
class OpenTrade:
    plan: TradePlan
    tp1_filled: bool = False
    break_even_set: bool = False
    trailing_stop: Optional[float] = None
    history: list = field(default_factory=list)


class RiskManager:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    # ---- sizing ------------------------------------------------------------

    def build_plan(
        self,
        side: str,
        entry: float,
        stop: float,
        equity: float,
        min_notional: Optional[float] = None,
        step_size: Optional[float] = None,
    ) -> Optional[TradePlan]:
        cfg = self.config
        min_notional = float(min_notional if min_notional is not None else cfg.min_notional)
        step = float(step_size) if step_size else 0.0

        if equity < cfg.absolute_min_capital:
            logger.warning(
                "Refusing to size trade: equity $%.4f is below absolute_min_capital $%.2f",
                equity, cfg.absolute_min_capital,
            )
            return None

        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            logger.debug("Zero stop distance — rejecting plan")
            return None

        base_risk_pct = min(cfg.risk_per_trade, cfg.max_risk_per_trade)
        risk_usd = equity * base_risk_pct
        qty = risk_usd / stop_distance
        notional = qty * entry
        adjustment_note = f"base risk {base_risk_pct*100:.2f}%"

        logger.debug(
            "Sizing attempt: equity=$%.4f base_risk=%.2f%% stop_dist=%.6f qty=%.4f notional=$%.4f min_notional=$%.2f",
            equity, base_risk_pct * 100, stop_distance, qty, notional, min_notional,
        )

        # Small-account escape: widen risk to meet exchange min-notional.
        if notional < min_notional:
            if not cfg.small_account_mode:
                logger.info(
                    "Plan rejected: notional $%.4f < min_notional $%.2f and small_account_mode disabled",
                    notional, min_notional,
                )
                return None

            required_qty = min_notional / entry
            required_risk_usd = required_qty * stop_distance
            required_risk_pct = required_risk_usd / equity if equity > 0 else 1.0

            if required_risk_pct > cfg.small_account_max_risk:
                logger.warning(
                    "Small-account sizing would require %.1f%% risk, exceeding cap %.1f%% — skipping trade",
                    required_risk_pct * 100, cfg.small_account_max_risk * 100,
                )
                return None

            qty = required_qty
            notional = qty * entry
            risk_usd = required_risk_usd
            adjustment_note = (
                f"adaptive risk widened to {required_risk_pct*100:.2f}% to meet "
                f"min notional ${min_notional:.2f}"
            )
            logger.info(
                "Small-account mode: widening risk to %.2f%% (risk_usd=$%.4f) to satisfy min_notional",
                required_risk_pct * 100, risk_usd,
            )

        # Round qty down to the exchange step size (log if it dropped us below min-notional)
        if step > 0:
            stepped_qty = (qty // step) * step
            if stepped_qty * entry < min_notional and cfg.small_account_mode:
                stepped_qty = stepped_qty + step  # round up by one step
            qty = max(step, stepped_qty)
            notional = qty * entry
            risk_usd = qty * stop_distance

        risk_pct_used = risk_usd / equity if equity > 0 else 0.0
        margin_required = notional / max(cfg.leverage, 1)

        if margin_required > equity:
            logger.warning(
                "Plan requires $%.4f margin but equity is $%.4f (leverage=%dx). "
                "Increase leverage or add funds.",
                margin_required, equity, cfg.leverage,
            )
            return None

        tp1_distance = stop_distance * cfg.tp1_rr
        tp1_price = entry + tp1_distance if side == "long" else entry - tp1_distance
        tp1_qty = qty * cfg.tp1_fraction
        runner_qty = qty - tp1_qty

        if step > 0:
            tp1_qty = max(step, (tp1_qty // step) * step)
            runner_qty = max(0.0, qty - tp1_qty)

        plan = TradePlan(
            side=side,
            entry=entry,
            stop=stop,
            tp1=tp1_price,
            tp1_quantity=tp1_qty,
            runner_quantity=runner_qty,
            total_quantity=qty,
            risk_usd=risk_usd,
            notional=notional,
            risk_pct_used=risk_pct_used,
            margin_required=margin_required,
            reason=adjustment_note,
        )
        logger.info(
            "Plan built: side=%s qty=%.4f entry=%.6f stop=%.6f tp1=%.6f "
            "risk=$%.4f (%.2f%%) notional=$%.4f margin=$%.4f [%s]",
            side, qty, entry, stop, tp1_price, risk_usd, risk_pct_used * 100,
            notional, margin_required, adjustment_note,
        )
        return plan

    # ---- live trade management --------------------------------------------

    def tp1_hit(self, trade: OpenTrade, current_price: float) -> bool:
        plan = trade.plan
        if trade.tp1_filled:
            return False
        if plan.side == "long":
            return current_price >= plan.tp1
        return current_price <= plan.tp1

    def stop_hit(self, trade: OpenTrade, current_price: float) -> bool:
        plan = trade.plan
        stop = trade.trailing_stop if trade.trailing_stop is not None else plan.stop
        if plan.side == "long":
            return current_price <= stop
        return current_price >= stop

    def advance_to_break_even(self, trade: OpenTrade) -> None:
        plan = trade.plan
        trade.trailing_stop = plan.entry
        trade.break_even_set = True
        trade.tp1_filled = True
        logger.info("Stop advanced to break-even at %.6f", plan.entry)

    def update_trailing_stop(self, trade: OpenTrade, psar: float) -> bool:
        """Move the runner's trailing stop in the favorable direction only."""
        if not trade.tp1_filled:
            return False
        plan = trade.plan
        current = trade.trailing_stop or plan.stop
        if plan.side == "long" and psar > current:
            trade.trailing_stop = psar
            logger.info("Trailing stop (long) tightened to %.6f via Parabolic SAR", psar)
            return True
        if plan.side == "short" and psar < current:
            trade.trailing_stop = psar
            logger.info("Trailing stop (short) tightened to %.6f via Parabolic SAR", psar)
            return True
        return False
