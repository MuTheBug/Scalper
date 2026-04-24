"""Thin wrapper around python-binance for Futures trading.

Encapsulates the calls the scalper needs: klines, order book, recent
trades, account balance, and placing/cancelling orders. Supports the
Binance Futures testnet via the ``testnet`` flag.
"""

from __future__ import annotations

import logging
import math
from typing import List, Optional

import pandas as pd
from binance.client import Client
from binance.enums import (
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_MARKET,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
    TIME_IN_FORCE_GTC,
)

from ..strategy.order_flow import OrderBookSnapshot

logger = logging.getLogger(__name__)


class BinanceFuturesClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        symbol: str = "DOGEUSDT",
    ) -> None:
        self.client = Client(api_key, api_secret, testnet=testnet)
        if testnet:
            # python-binance testnet URL is for spot; override for futures.
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        self.symbol = symbol
        self._price_precision: Optional[int] = None
        self._qty_precision: Optional[int] = None
        self._min_notional: Optional[float] = None
        self._step_size: Optional[float] = None
        self._tick_size: Optional[float] = None

    # -- metadata ------------------------------------------------------------

    def load_symbol_filters(self) -> None:
        info = self.client.futures_exchange_info()
        for sym in info["symbols"]:
            if sym["symbol"] != self.symbol:
                continue
            self._price_precision = int(sym["pricePrecision"])
            self._qty_precision = int(sym["quantityPrecision"])
            for f in sym["filters"]:
                ftype = f.get("filterType", "")
                if ftype == "LOT_SIZE":
                    self._step_size = float(f["stepSize"])
                elif ftype == "PRICE_FILTER":
                    self._tick_size = float(f["tickSize"])
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    # Binance uses different keys across endpoints/timeframes
                    notional = f.get("notional") or f.get("minNotional") or f.get("value")
                    if notional is not None:
                        self._min_notional = float(notional)
            logger.debug(
                "Loaded filters for %s: price_prec=%d qty_prec=%d step=%s tick=%s min_notional=%s",
                self.symbol, self._price_precision, self._qty_precision,
                self._step_size, self._tick_size, self._min_notional,
            )
            return
        raise ValueError(f"Symbol {self.symbol} not found on Binance Futures")

    @property
    def price_precision(self) -> int:
        return self._price_precision or 5

    @property
    def qty_precision(self) -> int:
        return self._qty_precision or 0

    @property
    def min_notional(self) -> float:
        return self._min_notional or 5.0

    def round_price(self, price: float) -> float:
        if self._tick_size:
            return math.floor(price / self._tick_size) * self._tick_size
        return round(price, self.price_precision)

    def round_qty(self, qty: float) -> float:
        if self._step_size:
            return math.floor(qty / self._step_size) * self._step_size
        return round(qty, self.qty_precision)

    # -- market data ---------------------------------------------------------

    def klines(self, interval: str, limit: int = 500) -> pd.DataFrame:
        raw = self.client.futures_klines(symbol=self.symbol, interval=interval, limit=limit)
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df

    def order_book(self, depth: int = 50) -> OrderBookSnapshot:
        data = self.client.futures_order_book(symbol=self.symbol, limit=max(5, depth))
        bids = [(float(p), float(q)) for p, q in data["bids"]]
        asks = [(float(p), float(q)) for p, q in data["asks"]]
        return OrderBookSnapshot(bids=bids, asks=asks)

    def recent_trades(self, limit: int = 500) -> List[dict]:
        return self.client.futures_aggregate_trades(symbol=self.symbol, limit=limit)

    # -- account -------------------------------------------------------------

    def available_balance(self, asset: str = "USDT") -> float:
        balances = self.client.futures_account_balance()
        for b in balances:
            if b["asset"] == asset:
                return float(b["availableBalance"])
        return 0.0

    def set_leverage(self, leverage: int) -> None:
        try:
            self.client.futures_change_leverage(symbol=self.symbol, leverage=leverage)
        except Exception as exc:  # noqa: BLE001 — exchange may reject re-requests
            logger.warning("Leverage change rejected: %s", exc)

    # -- orders --------------------------------------------------------------

    def place_limit(self, side: str, quantity: float, price: float, post_only: bool = True) -> dict:
        return self.client.futures_create_order(
            symbol=self.symbol,
            side="BUY" if side == "long" else "SELL",
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce="GTX" if post_only else TIME_IN_FORCE_GTC,
            quantity=self.round_qty(quantity),
            price=f"{self.round_price(price):.{self.price_precision}f}",
        )

    def place_market(self, side: str, quantity: float, reduce_only: bool = False) -> dict:
        return self.client.futures_create_order(
            symbol=self.symbol,
            side="BUY" if side == "long" else "SELL",
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=self.round_qty(quantity),
            reduceOnly=str(reduce_only).lower(),
        )

    def place_stop_market(self, side: str, stop_price: float, close_position: bool = True) -> dict:
        """Side is the side that *closes* the position."""
        return self.client.futures_create_order(
            symbol=self.symbol,
            side="SELL" if side == "long" else "BUY",
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=f"{self.round_price(stop_price):.{self.price_precision}f}",
            closePosition=str(close_position).lower(),
            workingType="MARK_PRICE",
        )

    def place_take_profit(self, side: str, tp_price: float, quantity: float) -> dict:
        return self.client.futures_create_order(
            symbol=self.symbol,
            side="SELL" if side == "long" else "BUY",
            type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=f"{self.round_price(tp_price):.{self.price_precision}f}",
            quantity=self.round_qty(quantity),
            reduceOnly="true",
            workingType="MARK_PRICE",
        )

    def cancel_all(self) -> None:
        try:
            self.client.futures_cancel_all_open_orders(symbol=self.symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cancel all failed: %s", exc)

    def position(self) -> dict:
        positions = self.client.futures_position_information(symbol=self.symbol)
        return positions[0] if positions else {}
