"""WP-R (step 2 routing) developer unit tests: validators.py only.

Covers the extended NATIVE_PROVIDER_TOOLS table, the resulting hint text and
the new native_symbol_prefix() helper. Pure functions; no network involved.
"""
from __future__ import annotations

import pytest

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.utils import validators as v

HL_TOOLS_STEP2 = (
    "hyperliquid_market_overview",
    "hyperliquid_funding_rates",
    "hyperliquid_orderbook",
    "top_gainers",
    "top_losers",
    "coin_analysis",
    "multi_timeframe_analysis",
    "smart_money_analysis",
    "backtest_strategy",
    "compare_strategies",
    "walk_forward_backtest_strategy",
)

HINT_STEP2 = (
    "HYPERLIQUID is a native (non-TradingView) perpetuals venue; it is supported "
    "only by: hyperliquid_market_overview, hyperliquid_funding_rates, "
    "hyperliquid_orderbook, top_gainers, top_losers, coin_analysis, "
    "multi_timeframe_analysis, smart_money_analysis, backtest_strategy, "
    "compare_strategies, walk_forward_backtest_strategy."
)


# ── NATIVE_PROVIDER_TOOLS / hint ─────────────────────────────────────────────

def test_native_provider_tools_is_the_ordered_11_tuple():
    assert v.NATIVE_PROVIDER_TOOLS["hyperliquid"] == HL_TOOLS_STEP2
    assert len(set(HL_TOOLS_STEP2)) == 11


def test_native_support_hint_lists_all_11_tools():
    assert v.native_support_hint("hyperliquid") == HINT_STEP2
    assert v.native_support_hint(" HYPERLIQUID ") == HINT_STEP2


def test_unsupported_exchange_error_carries_full_tool_list():
    with pytest.raises(ScreenerServiceError) as ei:
        v.validate_exchange("HYPERLIQUID")
    assert ei.value.code == ErrorCode.UNSUPPORTED_EXCHANGE
    assert ei.value.extra["supported_tools"] == list(HL_TOOLS_STEP2)
    assert HINT_STEP2 in str(ei.value)


# ── native_symbol_prefix ─────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol", [
    "HYPERLIQUID:BTC",
    " hyperliquid:kpepe ",
    "Hyperliquid:kPEPE",
    "HYPERLIQUID :ETH",
    "HYPERLIQUID:",
    "hyperliquid:BTC:extra",
])
def test_native_symbol_prefix_hits(symbol):
    assert v.native_symbol_prefix(symbol) == "hyperliquid"


@pytest.mark.parametrize("symbol", [
    None,
    "",
    "   ",
    "BTC",
    "HYPERLIQUID",
    "kPEPE",
    "BINANCE:BTCUSDT",
    "KUCOIN:HYPEUSDT",
    "EGX:COMI",
    "TVC:GOLD",
    ":BTC",
    "BTC:HYPERLIQUID",
    "HYPER:BTC",
])
def test_native_symbol_prefix_misses(symbol):
    assert v.native_symbol_prefix(symbol) is None


def test_native_symbol_prefix_agrees_with_reject_native_symbol_prefix():
    """Whatever native_symbol_prefix flags, reject_native_symbol_prefix rejects."""
    for sym in ["HYPERLIQUID:BTC", " hyperliquid:kpepe ", "HYPERLIQUID :ETH"]:
        assert v.native_symbol_prefix(sym) == "hyperliquid"
        with pytest.raises(ScreenerServiceError) as ei:
            v.reject_native_symbol_prefix(sym)
        assert ei.value.code == ErrorCode.UNSUPPORTED_EXCHANGE
    for sym in [None, "", "BTC", "HYPERLIQUID", "BINANCE:BTCUSDT"]:
        assert v.native_symbol_prefix(sym) is None
        v.reject_native_symbol_prefix(sym)  # no-op


def test_reject_native_symbol_prefix_unchanged():
    with pytest.raises(ScreenerServiceError) as ei:
        v.reject_native_symbol_prefix("hyperliquid:btc")
    err = ei.value
    assert err.code == ErrorCode.UNSUPPORTED_EXCHANGE
    assert err.extra["exchange"] == "HYPERLIQUID"
    assert err.extra["retryable"] is False
    assert str(err).startswith("Exchange 'HYPERLIQUID' is not supported by this tool.")


# ── server.py dispatch (edit 1) ──────────────────────────────────────────────
# Thin-branch checks only: every downstream entry point is replaced at the
# server-module name, so no provider, TradingView or Yahoo code runs.

from tradingview_mcp import server  # noqa: E402


class _Calls:
    def __init__(self):
        self.log = []

    def rec(self, name, ret):
        def fn(*a, **kw):
            self.log.append((name, a, kw))
            return ret
        return fn

    def arec(self, name, ret):
        async def fn(*a, **kw):
            self.log.append((name, a, kw))
            return ret
        return fn

    def trip(self, name):
        def fn(*a, **kw):
            raise AssertionError(f"tripwire hit: server.{name}")
        return fn


@pytest.fixture
def calls(monkeypatch):
    c = _Calls()
    for name in ("fetch_trending_analysis", "analyze_coin", "run_multi_timeframe_analysis",
                 "analyze_smart_money", "run_backtest", "_compare_strategies",
                 "walk_forward_backtest", "hyperliquid_top_movers", "analyze_hyperliquid_coin",
                 "hyperliquid_smart_money", "hyperliquid_run_backtest",
                 "hyperliquid_compare_strategies", "hyperliquid_walk_forward_backtest",
                 "get_price_async"):
        monkeypatch.setattr(server, name, c.trip(name))
    for name in ("hyperliquid_top_movers_async", "analyze_hyperliquid_mtf"):
        monkeypatch.setattr(server, name, c.trip(name))
    return c


async def test_top_gainers_hl_dispatches_async(calls, monkeypatch):
    monkeypatch.setattr(server, "hyperliquid_top_movers_async", calls.arec("hl", [{"source": "hyperliquid"}]))
    out = await server.top_gainers(exchange=" Hyperliquid ", timeframe="1d", limit=999)
    assert out == [{"source": "hyperliquid"}]
    # Raw timeframe is passed; the movers module does its own 1D check.
    assert calls.log == [("hl", ("1d",), {"losers": False, "limit": 50})]


def test_top_losers_hl_dispatches_sync(calls, monkeypatch):
    monkeypatch.setattr(server, "hyperliquid_top_movers", calls.rec("hl", []))
    assert server.top_losers(exchange="HYPERLIQUID", timeframe="1D", limit=0) == []
    assert calls.log == [("hl", ("1D",), {"losers": True, "limit": 1})]


# Movers timeframe hint (D1): on the Hyperliquid path the 1D-only check runs
# before the generic validate_timeframe, so even a timeframe TradingView does
# not know ("30m") gets valid_timeframes == ["1D"]. Real movers module, zero HTTP.

_MOVERS_BAD_TFS = ["30m", "15m", "5m", "1W", "", "bogus"]


def _assert_movers_tf_error(out, tf):
    err = out["error"]
    assert err["code"] == "INVALID_TIMEFRAME", tf
    assert err["valid_timeframes"] == ["1D"]
    assert err["exchange"] == "HYPERLIQUID"
    assert err["retryable"] is False


@pytest.mark.parametrize("tf", _MOVERS_BAD_TFS)
async def test_top_gainers_hl_timeframe_hint(hl_mock, no_network, tf):
    out = await server.top_gainers(exchange="HYPERLIQUID", timeframe=tf)
    _assert_movers_tf_error(out, tf)
    assert hl_mock.count == 0


@pytest.mark.parametrize("tf", _MOVERS_BAD_TFS)
def test_top_losers_hl_timeframe_hint(hl_mock, no_network, tf):
    out = server.top_losers(exchange="HYPERLIQUID", timeframe=tf)
    _assert_movers_tf_error(out, tf)
    assert hl_mock.count == 0


async def test_top_gainers_default_timeframe_gets_movers_hint(hl_mock, no_network):
    _assert_movers_tf_error(await server.top_gainers(exchange="HYPERLIQUID"), "15m")
    assert hl_mock.count == 0


def test_top_losers_tv_path_still_validates_generically(calls):
    out = server.top_losers(exchange="KUCOIN", timeframe="30m")
    assert out["error"]["code"] == "INVALID_TIMEFRAME"
    assert out["error"]["valid_timeframes"] == sorted(v.ALLOWED_TIMEFRAMES)


async def test_top_gainers_tv_path_still_validates_generically(calls):
    out = await server.top_gainers(exchange="BINANCE", timeframe="30m")
    assert out["error"]["valid_timeframes"] == sorted(v.ALLOWED_TIMEFRAMES)


def test_top_losers_tv_path_normalizes_timeframe(calls, monkeypatch):
    monkeypatch.setattr(server, "fetch_trending_analysis", calls.rec("tv", []))
    assert server.top_losers(exchange="KUCOIN", timeframe="1d") == []
    assert calls.log == [("tv", ("kucoin",), {"timeframe": "1D", "limit": 25, "sort": "asc"})]


@pytest.mark.parametrize("symbol,exchange", [
    ("BTC", "HYPERLIQUID"),
    ("HYPERLIQUID:BTC", "KUCOIN"),
    ("hyperliquid:eth", "BINANCE"),
])
def test_coin_analysis_routes_native(calls, monkeypatch, symbol, exchange):
    monkeypatch.setattr(server, "analyze_hyperliquid_coin", calls.rec("hl", {"source": "hyperliquid"}))
    assert server.coin_analysis(symbol, exchange, "1h") == {"source": "hyperliquid"}
    assert calls.log == [("hl", (symbol, "1h"), {})]


def test_coin_analysis_tv_path_unchanged(calls, monkeypatch):
    monkeypatch.setattr(server, "analyze_coin", calls.rec("tv", {"ok": True}))
    assert server.coin_analysis("BTCUSDT", "BINANCE", "1h") == {"ok": True}
    assert calls.log == [("tv", ("BTCUSDT", "binance", "1h"), {})]


def test_coin_analysis_unknown_exchange_still_invalid(calls):
    out = server.coin_analysis("BTC", "KRAKEN", "1h")
    assert out["error"]["code"] == "INVALID_EXCHANGE"


@pytest.mark.parametrize("symbol,exchange", [("BTC", "hyperliquid"), ("HYPERLIQUID:BTC", "KUCOIN")])
async def test_mtf_routes_native(calls, monkeypatch, symbol, exchange):
    monkeypatch.setattr(server, "analyze_hyperliquid_mtf", calls.arec("hl", {"source": "hyperliquid"}))
    assert await server.multi_timeframe_analysis(symbol, exchange) == {"source": "hyperliquid"}
    assert calls.log == [("hl", (symbol,), {})]


@pytest.mark.parametrize("symbol,exchange", [("BTC", "HYPERLIQUID"), ("HYPERLIQUID:kPEPE", "EGX")])
def test_smart_money_routes_native(calls, monkeypatch, symbol, exchange):
    monkeypatch.setattr(server, "hyperliquid_smart_money", calls.rec("hl", {"source": "hyperliquid"}))
    assert server.smart_money_analysis(symbol, exchange, "1y", "1h") == {"source": "hyperliquid"}
    assert calls.log == [("hl", (symbol, "1y", "1h"), {})]


def test_smart_money_egx_path_unchanged(calls, monkeypatch):
    monkeypatch.setattr(server, "analyze_smart_money", calls.rec("yf", {"ok": True}))
    assert server.smart_money_analysis("COMI") == {"ok": True}
    assert calls.log == [("yf", ("COMI", "EGX", "6mo", "1d"), {})]


def test_backtests_route_on_prefix_only(calls, monkeypatch):
    monkeypatch.setattr(server, "hyperliquid_run_backtest", calls.rec("bt", {}))
    monkeypatch.setattr(server, "hyperliquid_compare_strategies", calls.rec("cmp", {}))
    monkeypatch.setattr(server, "hyperliquid_walk_forward_backtest", calls.rec("wf", {}))
    server.backtest_strategy("HYPERLIQUID:BTC", "rsi", "6mo", 500.0, 0.2, 0.1, "1h", True, True)
    server.compare_strategies("hyperliquid:kpepe", "3mo", 500.0, "1h")
    server.walk_forward_backtest_strategy("HYPERLIQUID:ETH", "macd", "1y", 500.0, 0.2, 0.1, 4, 0.6, "1h")
    assert calls.log == [
        ("bt", ("HYPERLIQUID:BTC", "rsi", "6mo", 500.0, 0.2, 0.1, "1h", True, True), {}),
        ("cmp", ("hyperliquid:kpepe", "3mo", 500.0, "1h"), {}),
        ("wf", ("HYPERLIQUID:ETH", "macd", "1y", 500.0, 0.2, 0.1, 4, 0.6, "1h"), {}),
    ]


def test_backtests_bare_symbol_stays_on_yahoo(calls, monkeypatch):
    monkeypatch.setattr(server, "run_backtest", calls.rec("bt", {}))
    monkeypatch.setattr(server, "_compare_strategies", calls.rec("cmp", {}))
    monkeypatch.setattr(server, "walk_forward_backtest", calls.rec("wf", {}))
    server.backtest_strategy("BTC", "rsi")
    server.compare_strategies("BTC")
    server.walk_forward_backtest_strategy("BTC", "rsi")
    assert [name for name, _, _ in calls.log] == ["bt", "cmp", "wf"]


async def test_yahoo_price_rejects_prefix_but_passes_others(calls, monkeypatch):
    out = await server.yahoo_price(" Hyperliquid:BTC ")
    assert out["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    monkeypatch.setattr(server, "get_price_async", calls.arec("yf", {"ok": True}))
    assert await server.yahoo_price("BTC-USD") == {"ok": True}
    assert calls.log == [("yf", ("BTC-USD",), {})]
