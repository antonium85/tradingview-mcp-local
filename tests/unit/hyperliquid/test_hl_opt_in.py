"""Opted-in tools: refusals, Hyperliquid serving, prefix routing, client discipline
(spec step-2 section 5.1 items 7-12)."""
from __future__ import annotations

import pytest

from tradingview_mcp import server
from tradingview_mcp.core.services import (
    backtest_service,
    screener_provider,
    screener_service,
    smart_money_service,
)
from tradingview_mcp.core.services import hyperliquid_service as svc
from tradingview_mcp.core.utils import validators


class _Tripwires:
    def __init__(self):
        self.hits: list[str] = []

    def arm(self, monkeypatch):
        def make(name):
            def _boom(*a, **k):
                self.hits.append(name)
                raise AssertionError(f"tripwire hit: {name}")
            return _boom

        for mod, attr in [
            (screener_service, "get_multiple_analysis"),
            (screener_provider, "_scan_with_retry"),
            (screener_provider, "fetch_atr_for_ticker"),
            (server, "fetch_trending_analysis"),
            (server, "analyze_coin"),
            (server, "run_multi_timeframe_analysis"),
            (backtest_service, "_fetch_ohlcv"),
            (smart_money_service, "_fetch_ohlcv"),
            (server, "analyze_smart_money"),
            (server, "run_backtest"),
            (server, "_compare_strategies"),
            (server, "walk_forward_backtest"),
        ]:
            monkeypatch.setattr(mod, attr, make(f"{mod.__name__.rsplit('.', 1)[-1]}.{attr}"), raising=False)


@pytest.fixture
def tw(monkeypatch):
    t = _Tripwires()
    t.arm(monkeypatch)
    return t


async def call(name, args):
    tool = server.mcp._tool_manager.get_tool(name)
    return await tool.run(args)


HL = "HYPERLIQUID"

# ── 7. refusals ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tf", ["5m", "15m", "1h", "4h", "1W", "1M"])
@pytest.mark.parametrize("name", ["top_gainers", "top_losers"])
async def test_movers_reject_non_1d(name, tf, hl_mock, no_network, tw):
    res = await call(name, {"exchange": HL, "timeframe": tf})
    err = res["error"]
    assert err["code"] == "INVALID_TIMEFRAME"
    assert err["valid_timeframes"] == ["1D"]
    assert err["exchange"] == HL
    assert err["retryable"] is False
    assert "1D" in err["message"] and "24h" in err["message"]
    assert hl_mock.count == 0 and no_network == [] and tw.hits == []


async def test_top_gainers_default_timeframe_rejected(hl_mock, no_network, tw):
    res = await call("top_gainers", {"exchange": HL})
    assert res["error"]["code"] == "INVALID_TIMEFRAME"
    assert hl_mock.count == 0 and no_network == [] and tw.hits == []


async def test_coin_analysis_bad_timeframe(hl_mock, no_network, tw):
    res = await call("coin_analysis", {"symbol": "BTC", "exchange": HL, "timeframe": "30m"})
    assert res["error"]["code"] == "INVALID_TIMEFRAME"
    assert hl_mock.count == 0 and no_network == [] and tw.hits == []


async def test_mtf_rejects_foreign_prefix(hl_mock, no_network, tw):
    res = await call("multi_timeframe_analysis", {"symbol": "BINANCE:BTCUSDT", "exchange": HL})
    assert res["error"]["code"] == "INVALID_PARAMETER"
    assert hl_mock.of_type("candleSnapshot") == []
    assert no_network == [] and tw.hits == []


@pytest.mark.parametrize("extra", [{"interval": "4h"}, {"period": "5y"}], ids=["interval", "period"])
async def test_smart_money_rejects_bad_params(extra, hl_mock, no_network, tw):
    res = await call("smart_money_analysis", {"symbol": "BTC", "exchange": HL, **extra})
    assert res["error"]["code"] == "INVALID_PARAMETER"
    assert hl_mock.count == 0 and no_network == [] and tw.hits == []


@pytest.mark.parametrize("name,args", [
    ("backtest_strategy", {"symbol": "HYPERLIQUID:BTC", "strategy": "rsi", "interval": "4h"}),
    ("backtest_strategy", {"symbol": "HYPERLIQUID:BTC", "strategy": "nope"}),
    ("compare_strategies", {"symbol": "HYPERLIQUID:BTC", "period": "5y"}),
], ids=["bad-interval", "bad-strategy", "bad-period"])
async def test_backtests_legacy_string_errors(name, args, hl_mock, no_network, tw):
    res = await call(name, args)
    assert isinstance(res["error"], str), res
    assert hl_mock.of_type("candleSnapshot") == []
    assert no_network == [] and tw.hits == []


# ── 8. serving ───────────────────────────────────────────────────────────────

SERVE = [
    ("top_gainers", {"exchange": HL, "timeframe": "1D"}),
    ("top_losers", {"exchange": "hyperliquid", "timeframe": "1d"}),
    ("coin_analysis", {"symbol": "BTC", "exchange": HL, "timeframe": "1h"}),
    ("multi_timeframe_analysis", {"symbol": "BTC", "exchange": HL}),
    ("smart_money_analysis", {"symbol": "BTC", "exchange": HL}),
    ("backtest_strategy", {"symbol": "HYPERLIQUID:BTC", "strategy": "rsi"}),
    ("compare_strategies", {"symbol": "HYPERLIQUID:BTC"}),
    ("walk_forward_backtest_strategy", {"symbol": "HYPERLIQUID:BTC", "strategy": "rsi"}),
]


@pytest.mark.parametrize("name,args", SERVE, ids=[s[0] for s in SERVE])
async def test_opted_in_tools_serve_hyperliquid(name, args, hl_mock, no_network, tw):
    res = await call(name, args)
    if isinstance(res, list):
        assert res and all(r["source"] == "hyperliquid" for r in res)
    else:
        assert "error" not in res, res
        assert res["source"] == "hyperliquid"
    assert {b["type"] for b in hl_mock.requests} <= {"metaAndAssetCtxs", "candleSnapshot"}
    assert hl_mock.count >= 1
    assert no_network == [] and tw.hits == []


# ── 9. prefix routing ────────────────────────────────────────────────────────

async def test_coin_analysis_prefix_default_exchange(hl_mock, no_network, tw):
    res = server.coin_analysis("HYPERLIQUID:BTC")
    assert "error" not in res, res
    assert res["source"] == "hyperliquid" and res["coin"] == "BTC"
    assert no_network == [] and tw.hits == []


async def test_coin_analysis_prefix_beats_exchange(hl_mock, no_network, tw):
    res = server.coin_analysis("hyperliquid:eth", exchange="BINANCE")
    assert "error" not in res, res
    assert res["source"] == "hyperliquid" and res["coin"] == "ETH"
    assert no_network == [] and tw.hits == []


async def test_mtf_prefix_routes(hl_mock, no_network, tw):
    res = await server.multi_timeframe_analysis("HYPERLIQUID:BTC")
    assert "error" not in res, res
    assert res["source"] == "hyperliquid" and res["coin"] == "BTC"
    assert no_network == [] and tw.hits == []


async def test_smart_money_prefix_routes(hl_mock, no_network, tw):
    res = server.smart_money_analysis("HYPERLIQUID:kPEPE")
    if hasattr(res, "__await__"):
        res = await res
    assert "error" not in res, res
    assert res["source"] == "hyperliquid"
    assert res["symbol"] == "HYPERLIQUID:kPEPE"
    assert no_network == [] and tw.hits == []


# ── 10. client discipline ────────────────────────────────────────────────────

def _boom(*a, **k):
    raise AssertionError("wrong client used")


async def test_async_paths_do_not_use_sync_client(monkeypatch, hl_mock, no_network, tw):
    monkeypatch.setattr(svc, "get_client", _boom)
    r1 = await server.top_gainers(exchange=HL, timeframe="1D")
    assert isinstance(r1, list) and r1
    r2 = await server.multi_timeframe_analysis("BTC", HL)
    assert "error" not in r2, r2


async def test_sync_paths_do_not_use_async_client(monkeypatch, hl_mock, no_network, tw):
    monkeypatch.setattr(svc, "get_async_client", _boom)
    r1 = server.top_losers(exchange=HL, timeframe="1D")
    r2 = server.coin_analysis("BTC", HL, "1h")
    r3 = server.smart_money_analysis("BTC", HL)
    r4 = server.backtest_strategy("HYPERLIQUID:BTC", "rsi")
    for r in (r1, r2, r3, r4):
        if hasattr(r, "__await__"):
            r = await r
        assert not (isinstance(r, dict) and "error" in r), r


async def test_sync_tool_works_inside_running_loop(hl_mock, no_network, tw):
    res = server.coin_analysis("BTC", "HYPERLIQUID", "1h")
    assert "error" not in res, res


# ── 11. native_symbol_prefix ─────────────────────────────────────────────────

@pytest.mark.parametrize("arg,want", [
    ("HYPERLIQUID:BTC", "hyperliquid"),
    (" hyperliquid:kpepe ", "hyperliquid"),
    ("BINANCE:BTCUSDT", None),
    ("BTC", None),
    ("", None),
    (None, None),
    ("HYPERLIQUID", None),
])
def test_native_symbol_prefix(arg, want):
    assert validators.native_symbol_prefix(arg) == want


# ── 12. TradingView paths unchanged ──────────────────────────────────────────

async def test_top_gainers_kucoin_still_tradingview(monkeypatch, hl_mock):
    seen = []

    def fake(*a, **k):
        seen.append((a, k))
        return []

    monkeypatch.setattr(server, "fetch_trending_analysis", fake)
    res = await server.top_gainers(exchange="KUCOIN")
    assert seen, "fetch_trending_analysis not called"
    assert hl_mock.count == 0
    assert res == [] or isinstance(res, (list, dict))


def test_backtest_bare_symbol_goes_to_yahoo(monkeypatch, hl_mock):
    calls = []

    def rec(symbol, period, interval):
        calls.append(symbol)
        return [{"date": f"2024-01-{(i % 28) + 1:02d}", "open": 100 + i % 7, "high": 105 + i % 7,
                 "low": 95 + i % 7, "close": 100 + (i * 3) % 11, "volume": 1000.0}
                for i in range(60)]

    monkeypatch.setattr(backtest_service, "_fetch_ohlcv", rec)
    res = server.backtest_strategy("BTC", "rsi")
    assert calls == ["BTC"]
    assert res["data_source"] == "Yahoo Finance"
    assert hl_mock.count == 0
