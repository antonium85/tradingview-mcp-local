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

HL_NEW_TOOLS = ["hyperliquid_market_overview", "hyperliquid_funding_rates", "hyperliquid_orderbook"]

# Tools with an ``exchange`` argument that serve Hyperliquid (step 2).
NATIVE_OPT_IN: set[str] = {
    "top_gainers",
    "top_losers",
    "coin_analysis",
    "multi_timeframe_analysis",
    "smart_money_analysis",
}
# Tools with no ``exchange`` argument that route on a "HYPERLIQUID:" symbol prefix.
SYMBOL_PREFIX_OPT_IN: set[str] = {
    "backtest_strategy",
    "compare_strategies",
    "walk_forward_backtest_strategy",
}

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
    assert len(names) >= 9
    assert {"combined_analysis", "multi_agent_analysis", "bollinger_scan", "rating_filter",
            "volume_confirmation_analysis"} <= names
    assert names.isdisjoint(NATIVE_OPT_IN)


def test_opt_in_tables_match_hint_table():
    tools = server.mcp._tool_manager.list_tools()
    with_exchange = {t.name for t in tools if "exchange" in t.parameters.get("properties", {})}
    hint = set(NATIVE_PROVIDER_TOOLS["hyperliquid"])
    assert hint & with_exchange == NATIVE_OPT_IN
    assert hint == set(HL_NEW_TOOLS) | NATIVE_OPT_IN | SYMBOL_PREFIX_OPT_IN


def test_new_hyperliquid_tools_have_no_exchange_argument():
    by_name = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    for name in HL_NEW_TOOLS:
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

def test_volume_confirmation_rejects_hyperliquid_prefix(no_network, tripwires):
    result = server.volume_confirmation_analysis("HYPERLIQUID:BTC", exchange="KUCOIN")
    assert result["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    assert no_network == [] and tripwires.hits == []


async def test_combined_analysis_rejects_hyperliquid_prefix(monkeypatch, no_network, tripwires):
    seen: list[str] = []

    def make(name):
        def _boom(*a, **k):
            seen.append(name)
            raise AssertionError(f"tripwire hit: {name}")
        return _boom

    for attr in ("analyze_coin", "analyze_sentiment", "fetch_news_summary"):
        monkeypatch.setattr(server, attr, make(f"server.{attr}"), raising=False)
    result = server.combined_analysis("HYPERLIQUID:BTC", exchange="NASDAQ")
    if hasattr(result, "__await__"):
        result = await result
    assert result["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    assert seen == [] and no_network == [] and tripwires.hits == []


async def test_yahoo_price_rejects_hyperliquid_prefix(monkeypatch, no_network, tripwires):
    seen: list[str] = []

    def _boom(*a, **k):
        seen.append("server.get_price_async")
        raise AssertionError("tripwire hit: server.get_price_async")

    monkeypatch.setattr(server, "get_price_async", _boom, raising=False)
    result = server.yahoo_price("hyperliquid:btc")
    if hasattr(result, "__await__"):
        result = await result
    assert result["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    assert seen == [] and no_network == [] and tripwires.hits == []


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


# ── 11. auto-discovered symbol-prefix guard (tools with symbol, no exchange) ──

# D5 exemptions: tools that hand the raw symbol to Yahoo or Marketaux, never to
# TradingView (verified by reading each handler's upstream call).
#   stock_extended_hours          -> yahoo (get_extended_hours_price_async)
#   stock_options_chain           -> yahoo options service
#   stock_options_unusual_activity-> yahoo options service
#   market_sentiment              -> Marketaux/RSS (analyze_sentiment)
#   financial_news                -> Marketaux (fetch_news_summary)
SYMBOL_ONLY_D5_EXEMPT: set[str] = {
    "stock_extended_hours",
    "stock_options_chain",
    "stock_options_unusual_activity",
    "market_sentiment",
    "financial_news",
}


def _symbol_only_tools():
    out = []
    for t in server.mcp._tool_manager.list_tools():
        props = t.parameters.get("properties", {})
        if "symbol" in props and "exchange" not in props:
            if t.name in SYMBOL_PREFIX_OPT_IN or t.name in HL_NEW_TOOLS:
                continue
            out.append(t)
    return out


def test_symbol_only_discovery_sanity():
    names = {t.name for t in _symbol_only_tools()}
    assert "yahoo_price" in names
    assert SYMBOL_ONLY_D5_EXEMPT <= names | {"financial_news"}


def _tv_tripwires(monkeypatch, hits):
    def make(name):
        def _boom(*a, **k):
            hits.append(name)
            raise AssertionError(f"tripwire hit: {name}")
        return _boom

    for mod, attr in [
        (screener_service, "get_multiple_analysis"),
        (screener_provider, "_scan_with_retry"),
        (screener_provider, "fetch_atr_for_ticker"),
    ]:
        monkeypatch.setattr(mod, attr, make(f"{mod.__name__}.{attr}"))


@pytest.mark.parametrize("tool", _symbol_only_tools(), ids=lambda t: t.name)
async def test_symbol_only_tools_guard_hyperliquid_prefix(tool, monkeypatch, no_network, tripwires):
    hits: list[str] = []
    _tv_tripwires(monkeypatch, hits)
    exempt = tool.name in SYMBOL_ONLY_D5_EXEMPT
    args = {**{k: REQUIRED_ARGS.get(k, "BTC") for k in (tool.parameters.get("required") or [])},
            "symbol": "HYPERLIQUID:BTC"}
    try:
        result = await tool.run(args)
    except Exception as e:  # exempt tools may blow up on the disabled network
        if not exempt:
            raise
        result = {"raised": repr(e)}
    assert hits == [] and tripwires.hits == [], f"{tool.name} reached TradingView/Yahoo code"
    if not exempt:
        assert isinstance(result, dict) and "error" in result, result
        assert result["error"]["code"] == "UNSUPPORTED_EXCHANGE", (
            f"{tool.name}: unguarded symbol-only tool; guard it or add a commented D5 exemption")
