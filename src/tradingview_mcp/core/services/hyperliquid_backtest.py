"""Hyperliquid candle source for the backtest / smart-money services (Step 2, WP-BT).

The upstream services take a keyword-only ``fetch_ohlcv`` callable; these
wrappers hand them a per-call ``HyperliquidCandleFetcher`` and decorate the
result. Everything here is sync (FastMCP 1.x runs plain ``def`` tools on the
event loop), so the sync Hyperliquid client is used.
"""
from __future__ import annotations

from tradingview_mcp.core.errors import exception_to_envelope
from tradingview_mcp.core.services import backtest_service, smart_money_service
from tradingview_mcp.core.services import hyperliquid_service as hl_svc

# 24/7 market: calendar-day / calendar-hour annualization (decision D6).
HL_PERIODS_PER_YEAR: dict[str, int] = {"1d": 365, "1h": 8760}
DATA_SOURCE: str = "Hyperliquid"


class HyperliquidCandleFetcher:
    """One per tool call. Passed as fetch_ohlcv=... to backtest/smart-money services."""

    def __init__(self) -> None:
        self.meta: dict | None = None
        self.error: Exception | None = None

    def __call__(self, symbol: str, period: str, interval: str) -> list[dict]:
        try:
            result = hl_svc.fetch_hyperliquid_ohlcv(symbol, period, interval)
        except Exception as e:
            self.error = e
            raise
        self.meta = result
        return result["candles"]


def decorate_result(result: dict, fetcher: HyperliquidCandleFetcher, *,
                    context: str, periods_per_year: int | None = None) -> dict:
    if fetcher.error is not None:
        return exception_to_envelope(fetcher.error, context=context)
    if "error" in result:
        if isinstance(result["error"], dict):  # Yahoo-specific key is misleading here
            result["error"].pop("yahoo_symbol", None)
        return result
    meta = fetcher.meta
    if meta is None:  # service never fetched; nothing to decorate
        return result
    result.update({
        "symbol": f"HYPERLIQUID:{meta['coin']}",
        "coin": meta["coin"],
        "exchange": "HYPERLIQUID",
        "source": "hyperliquid",
        "data_source": DATA_SOURCE,
        "requested_bars": meta["requested_bars"],
        "returned_bars": meta["returned_bars"],
        "truncated": meta["truncated"],
        "data_warning": meta["warning"],
    })
    if periods_per_year is not None:
        result["periods_per_year"] = periods_per_year
    result.pop("yahoo_symbol", None)
    return result


def _ppy(interval: str) -> int | None:
    return HL_PERIODS_PER_YEAR.get((interval or "").strip().lower())


def hyperliquid_run_backtest(symbol: str, strategy: str, period: str = "1y",
        initial_capital: float = 10_000.0, commission_pct: float = 0.1, slippage_pct: float = 0.05,
        interval: str = "1d", include_trade_log: bool = False,
        include_equity_curve: bool = False) -> dict:
    try:
        f = HyperliquidCandleFetcher()
        ppy = _ppy(interval)
        result = backtest_service.run_backtest(
            symbol, strategy, period, initial_capital, commission_pct, slippage_pct,
            interval, include_trade_log, include_equity_curve,
            periods_per_year=ppy, fetch_ohlcv=f)
        return decorate_result(result, f, context="backtest_strategy", periods_per_year=ppy)
    except Exception as e:
        return exception_to_envelope(e, context="backtest_strategy")


def hyperliquid_compare_strategies(symbol: str, period: str = "1y",
        initial_capital: float = 10_000.0, interval: str = "1d") -> dict:
    try:
        f = HyperliquidCandleFetcher()
        ppy = _ppy(interval)
        result = backtest_service.compare_strategies(
            symbol, period, initial_capital, interval=interval,
            periods_per_year=ppy, fetch_ohlcv=f)
        return decorate_result(result, f, context="compare_strategies", periods_per_year=ppy)
    except Exception as e:
        return exception_to_envelope(e, context="compare_strategies")


def hyperliquid_walk_forward_backtest(symbol: str, strategy: str, period: str = "2y",
        initial_capital: float = 10_000.0, commission_pct: float = 0.1, slippage_pct: float = 0.05,
        n_splits: int = 3, train_ratio: float = 0.7, interval: str = "1d") -> dict:
    try:
        f = HyperliquidCandleFetcher()
        ppy = _ppy(interval)
        result = backtest_service.walk_forward_backtest(
            symbol, strategy, period, initial_capital, commission_pct, slippage_pct,
            n_splits, train_ratio, interval=interval,
            periods_per_year=ppy, fetch_ohlcv=f)
        return decorate_result(result, f, context="walk_forward_backtest_strategy",
                               periods_per_year=ppy)
    except Exception as e:
        return exception_to_envelope(e, context="walk_forward_backtest_strategy")


def hyperliquid_smart_money(symbol: str, period: str = "6mo", interval: str = "1d") -> dict:
    try:
        f = HyperliquidCandleFetcher()
        result = smart_money_service.analyze_smart_money(
            symbol, "HYPERLIQUID", period, interval, fetch_ohlcv=f)
        return decorate_result(result, f, context="smart_money_analysis")
    except Exception as e:
        return exception_to_envelope(e, context="smart_money_analysis")
