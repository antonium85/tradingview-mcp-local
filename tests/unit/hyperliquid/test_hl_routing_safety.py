"""Routing safety: a native venue (Hyperliquid) must never reach TradingView.

Spec step-1 section 5.1. Test 1 is parametrized over every MCP tool that has
an ``exchange`` argument, discovered at runtime, so tools added later are
covered automatically.
"""
from __future__ import annotations

import os

import pytest

from tradingview_mcp import server
from tradingview_mcp.core.errors import ScreenerServiceError
from tradingview_mcp.core.services import (
    backtest_service,
    screener_provider,
    screener_service,
    smart_money_service,
)
from tradingview_mcp.core.services.coinlist import exchanges_listing_symbol
from tradingview_mcp.core.utils.validators import (
    COINLIST_DIR,
    NATIVE_PROVIDER_TOOLS,
    normalize_tradingview_symbol,
)

HL_TOOLS = list(NATIVE_PROVIDER_TOOLS["hyperliquid"])

# Step 2 adds the names of tools that opt in to native venues.
NATIVE_OPT_IN: set[str] = set()

# Values for required parameters, by name. Extend when a tool adds a new one.
REQUIRED_ARGS: dict[str, object] = {"symbol": "BTC"}

EXCHANGE_VARIANTS = ["hyperliquid", "HYPERLIQUID", " Hyperliquid "]


def _exchange_tools():
    return [
        t
        for t in server.mcp._tool_manager.list_tools()
        if "exchange" in t.parameters.get("properties", {}) and t.name not in NATIVE_OPT_IN
    ]


def _required_args(tool) -> dict:
    out = {}
    for name in tool.parameters.get("required", []) or []:
        if name == "exchange":
            continue
        if name not in REQUIRED_ARGS:
            pytest.fail(
                f"tool {tool.name!r} has required parameter {name!r} unknown to the "
                f"routing-safety test; extend REQUIRED_ARGS in {__file__}"
            )
        out[name] = REQUIRED_ARGS[name]
    return out


class _Tripwires:
    def __init__(self):
        self.hits: list[str] = []

    def arm(self, monkeypatch):
        def make(name):
            def _boom(*a, **k):
                self.hits.append(name)
                raise AssertionError(f"tripwire hit: {name}")
            return _boom

        for mod, attr, label in [
            (screener_service, "get_multiple_analysis", "screener_service.get_multiple_analysis"),
            (screener_provider, "_scan_with_retry", "screener_provider._scan_with_retry"),
            (backtest_service, "_fetch_ohlcv", "backtest_service._fetch_ohlcv"),
            (smart_money_service, "_fetch_ohlcv", "smart_money_service._fetch_ohlcv"),
            (server, "analyze_smart_money", "server.analyze_smart_money"),
        ]:
            monkeypatch.setattr(mod, attr, make(label))


@pytest.fixture
def tripwires(monkeypatch):
    tw = _Tripwires()
    tw.arm(monkeypatch)
    return tw


# ── 1. parametrized over every exchange-taking tool ──────────────────────────

@pytest.mark.parametrize("ex", EXCHANGE_VARIANTS, ids=["lower", "upper", "padded"])
@pytest.mark.parametrize("tool", _exchange_tools(), ids=lambda t: t.name)
async def test_every_exchange_tool_rejects_hyperliquid(tool, ex, no_network, tripwires):
    result = await tool.run({**_required_args(tool), "exchange": ex})
    assert isinstance(result, dict) and "error" in result, f"{tool.name}: not an envelope: {result!r}"
    err = result["error"]
    assert err["code"] == "UNSUPPORTED_EXCHANGE", f"{tool.name}: {err}"
    assert err["retryable"] is False
    assert err["exchange"] == "HYPERLIQUID"
    assert err["supported_tools"] == HL_TOOLS
    for name in HL_TOOLS:
        assert name in err["message"]
    assert no_network == [], f"{tool.name} touched the network: {no_network}"
    assert tripwires.hits == [], f"{tool.name} reached upstream code: {tripwires.hits}"


# ── 2. discovery sanity ──────────────────────────────────────────────────────

def test_discovery_finds_the_expected_tools():
    names = {t.name for t in _exchange_tools()}
    assert len(names) >= 14
    assert {"smart_money_analysis", "combined_analysis", "top_gainers", "multi_timeframe_analysis"} <= names


def test_new_hyperliquid_tools_have_no_exchange_argument():
    by_name = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in HL_TOOLS:
        assert name in by_name
        assert "exchange" not in by_name[name].parameters.get("properties", {})


# ── 3. hint names are real ───────────────────────────────────────────────────

def test_hint_tool_names_are_registered_tools():
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert set(HL_TOOLS) <= registered


# ── 4. no fallback to Hyperliquid ────────────────────────────────────────────

def _hl_coinlist() -> list[str]:
    with open(os.path.join(COINLIST_DIR, "hyperliquid.txt"), encoding="utf-8") as f:
        return [ln.strip().split(":", 1)[1] for ln in f if ln.strip()]


def test_btc_has_no_fallback_from_binance():
    assert screener_service.pick_fallback_exchange("BTC", "BINANCE") is None


def test_no_coinlist_coin_falls_back_to_hyperliquid():
    coins = _hl_coinlist()
    assert len(coins) >= 100
    for coin in coins:
        for req in ("BINANCE", "KUCOIN", "NASDAQ"):
            got = screener_service.pick_fallback_exchange(coin, req)
            assert got != "HYPERLIQUID", (coin, req)


def test_collision_symbol_falls_back_to_a_tradingview_venue():
    got = screener_service.pick_fallback_exchange("SOL", "NASDAQ")
    assert got is not None
    assert got != "HYPERLIQUID"


# ── 5. filter before cap ─────────────────────────────────────────────────────

def test_native_filtered_before_cap_of_six(monkeypatch):
    universe = sorted({"ASX", "BIST", "BURSA", "EGX", "HKEX", "HYPERLIQUID", "KUCOIN"})

    def fake(symbol, max_results=6):
        return universe[:max_results]

    monkeypatch.setattr(screener_service, "exchanges_listing_symbol", fake)
    assert screener_service.pick_fallback_exchange("X", "BINANCE") == "KUCOIN"


# ── 6. hint split on a TradingView miss ──────────────────────────────────────

def test_analyze_coin_miss_splits_native_hint(monkeypatch):
    seen: list = []

    def fake_gma(*a, **k):
        seen.extend(k.get("symbols") or [])
        return {}

    def no_lookup(*a, **k):
        raise AssertionError("lookup_tradingview_instrument must be skipped")

    monkeypatch.setattr(screener_service, "get_multiple_analysis", fake_gma)
    monkeypatch.setattr(screener_service, "_TA_AVAILABLE", True)
    monkeypatch.setattr(screener_service, "lookup_tradingview_instrument", no_lookup)

    result = screener_service.analyze_coin("BTC", "binance", "1h")
    err = result["error"]
    assert err["code"] == "SYMBOL_NOT_FOUND"
    assert not [s for s in seen if str(s).upper().startswith("HYPERLIQUID:")]
    assert err["listed_on"] == []
    assert err["native_listed_on"] == ["HYPERLIQUID"]
    assert err["native_supported_tools"] == HL_TOOLS
    assert "hyperliquid_market_overview" in err["message"]
    assert "verify the ticker" not in err["message"].lower()


# ── 7. collision symbol ──────────────────────────────────────────────────────

def test_collision_symbol_not_found_error(monkeypatch):
    def no_lookup(*a, **k):
        raise AssertionError("lookup must not run when listed_on is non-empty")

    monkeypatch.setattr(screener_service, "lookup_tradingview_instrument", no_lookup)
    env = screener_service.symbol_not_found_error("SOL", "NASDAQ")
    err = env["error"]
    assert err["listed_on"], "SOL is listed on TradingView venues"
    assert "HYPERLIQUID" not in err["listed_on"]
    assert err["native_listed_on"] == ["HYPERLIQUID"]


# ── 8. symbol-prefix guard ───────────────────────────────────────────────────

def test_coin_analysis_rejects_hyperliquid_prefix(no_network, tripwires):
    result = server.coin_analysis("HYPERLIQUID:BTC", exchange="KUCOIN")
    assert result["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    assert no_network == [] and tripwires.hits == []


async def test_multi_agent_analysis_rejects_hyperliquid_prefix(no_network, tripwires):
    result = server.multi_agent_analysis("hyperliquid:btc", exchange="KUCOIN")
    if hasattr(result, "__await__"):
        result = await result
    assert result["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    assert no_network == [] and tripwires.hits == []


def test_normalize_tradingview_symbol_rejects_native_prefix():
    with pytest.raises(ScreenerServiceError) as ei:
        normalize_tradingview_symbol("HYPERLIQUID:BTC", "kucoin")
    assert ei.value.to_envelope()["error"]["code"] == "UNSUPPORTED_EXCHANGE"


def test_normalize_tradingview_symbol_unchanged_for_other_prefixes():
    assert normalize_tradingview_symbol("BINANCE:BTCUSDT", "kucoin") == "BINANCE:BTCUSDT"


# ── 9. smart money stays open to other venues ────────────────────────────────

def test_smart_money_still_serves_other_venues(monkeypatch):
    sentinel = {"sentinel": True}
    monkeypatch.setattr(server, "analyze_smart_money", lambda *a, **k: sentinel)
    result = server.smart_money_analysis("COMI", exchange="EGX")
    if hasattr(result, "__await__"):
        import asyncio
        result = asyncio.run(result)
    assert result == sentinel


# ── 10. unchanged suggestions ────────────────────────────────────────────────

def test_listing_hints_still_see_hyperliquid():
    assert "HYPERLIQUID" in exchanges_listing_symbol("BTC")
    assert exchanges_listing_symbol("kpepe") == ["HYPERLIQUID"]
