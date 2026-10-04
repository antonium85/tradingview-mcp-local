"""WP-A (routing safety) developer unit tests.

Covers the validators helpers, the UNSUPPORTED_EXCHANGE error code and the
native-venue filtering in screener_service. Hermetic: coinlist lookups are
monkeypatched, and the TradingView symbol-search lookup is a tripwire.
"""
from __future__ import annotations

import pytest

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services import screener_service as ss
from tradingview_mcp.core.utils import validators as v

HL_TOOLS = [
    "hyperliquid_market_overview",
    "hyperliquid_funding_rates",
    "hyperliquid_orderbook",
]


def _fake_listing(names):
    """Fake exchanges_listing_symbol that honours max_results like the real one."""
    def fake(symbol, max_results=6):
        return sorted(names)[:max_results]
    return fake


def _lookup_tripwire(*a, **kw):
    raise AssertionError("lookup_tradingview_instrument must not be called")


# ── errors.py ────────────────────────────────────────────────────────────────

def test_unsupported_exchange_code():
    assert ErrorCode.UNSUPPORTED_EXCHANGE.value == "UNSUPPORTED_EXCHANGE"
    assert ErrorCode.UNSUPPORTED_EXCHANGE is not ErrorCode.INVALID_EXCHANGE


# ── validators.py: helpers ───────────────────────────────────────────────────

def test_native_provider_constants():
    assert v.NATIVE_PROVIDERS == frozenset({"hyperliquid"})
    assert v.NATIVE_PROVIDER_TOOLS["hyperliquid"] == tuple(HL_TOOLS)
    for name in v.NATIVE_PROVIDERS:
        assert name not in v.EXCHANGE_SCREENER
        assert name not in v._TA_ONLY_SCREENERS


@pytest.mark.parametrize("ex", ["hyperliquid", "HYPERLIQUID", " Hyperliquid ", "\tHYPERliquid\n"])
def test_is_native_provider_true(ex):
    assert v.is_native_provider(ex) is True


@pytest.mark.parametrize("ex", [None, "", "  ", "binance", "kucoin", "egx", "hyper"])
def test_is_native_provider_false(ex):
    assert v.is_native_provider(ex) is False


def test_native_support_hint_exact():
    expected = (
        "HYPERLIQUID is a native (non-TradingView) perpetuals venue; it is supported only by: "
        "hyperliquid_market_overview, hyperliquid_funding_rates, hyperliquid_orderbook."
    )
    assert v.native_support_hint("hyperliquid") == expected
    assert v.native_support_hint(" HyperLiquid ") == expected


def test_native_support_hint_non_native_is_empty():
    assert v.native_support_hint("binance") == ""
    assert v.native_support_hint("") == ""


# ── validators.py: validate_exchange / sanitize_exchange ────────────────────

def _assert_unsupported(err: ScreenerServiceError):
    assert err.code == ErrorCode.UNSUPPORTED_EXCHANGE
    assert err.extra["exchange"] == "HYPERLIQUID"
    assert err.extra["supported_tools"] == HL_TOOLS
    assert err.extra["retryable"] is False
    for tool in HL_TOOLS:
        assert tool in str(err)


@pytest.mark.parametrize("ex", ["hyperliquid", "HYPERLIQUID", " Hyperliquid "])
def test_validate_exchange_rejects_native_by_default(ex):
    with pytest.raises(ScreenerServiceError) as ei:
        v.validate_exchange(ex, "KUCOIN")
    _assert_unsupported(ei.value)
    assert str(ei.value).startswith(f"Exchange {ex!r} is not supported by this tool. ")


def test_validate_exchange_envelope_shape():
    with pytest.raises(ScreenerServiceError) as ei:
        v.validate_exchange("HYPERLIQUID")
    env = ei.value.to_envelope()["error"]
    assert env["code"] == "UNSUPPORTED_EXCHANGE"
    assert env["exchange"] == "HYPERLIQUID"
    assert env["supported_tools"] == HL_TOOLS
    assert env["retryable"] is False


@pytest.mark.parametrize("ex", ["hyperliquid", " HYPERLIQUID "])
def test_validate_exchange_allows_native_with_opt_in(ex):
    assert v.validate_exchange(ex, allow_native=True) == "hyperliquid"


def test_validate_exchange_unchanged_for_tv_venues():
    assert v.validate_exchange("BINANCE") == "binance"
    assert v.validate_exchange("oanda") == "oanda"
    assert v.validate_exchange("", "KUCOIN") == "KUCOIN"
    assert v.validate_exchange(None, "KUCOIN") == "KUCOIN"
    assert v.validate_exchange("BINANCE", allow_native=True) == "binance"


def test_invalid_exchange_list_hides_native_unless_opt_in():
    with pytest.raises(ScreenerServiceError) as ei:
        v.validate_exchange("KRAKEN")
    assert ei.value.code == ErrorCode.INVALID_EXCHANGE
    plain = ei.value.extra["valid_exchanges"]
    assert "hyperliquid" not in plain
    assert plain == sorted({*v.EXCHANGE_SCREENER, *v._TA_ONLY_SCREENERS})

    with pytest.raises(ScreenerServiceError) as ei:
        v.validate_exchange("KRAKEN", allow_native=True)
    assert ei.value.code == ErrorCode.INVALID_EXCHANGE
    opted = ei.value.extra["valid_exchanges"]
    assert "hyperliquid" in opted
    assert opted == sorted({*plain, "hyperliquid"})


def test_sanitize_exchange_native():
    assert v.sanitize_exchange("HYPERLIQUID", "KUCOIN") == "KUCOIN"
    assert v.sanitize_exchange("HYPERLIQUID", "KUCOIN", allow_native=True) == "hyperliquid"
    assert v.sanitize_exchange("binance") == "binance"
    assert v.sanitize_exchange("kraken") == "kucoin"


# ── validators.py: reject_native_exchange ───────────────────────────────────

@pytest.mark.parametrize("ex", ["EGX", "binance", "", None, "KRAKEN"])
def test_reject_native_exchange_passes_others(ex):
    assert v.reject_native_exchange(ex) is None


def test_reject_native_exchange_raises_same_error():
    with pytest.raises(ScreenerServiceError) as ei:
        v.reject_native_exchange(" Hyperliquid ")
    _assert_unsupported(ei.value)


# ── validators.py: symbol-prefix guard (R4) ─────────────────────────────────

@pytest.mark.parametrize("sym", ["HYPERLIQUID:BTC", "hyperliquid:btc", " HyperLiquid:kPEPE "])
def test_normalize_tradingview_symbol_rejects_native_prefix(sym):
    with pytest.raises(ScreenerServiceError) as ei:
        v.normalize_tradingview_symbol(sym, "kucoin")
    _assert_unsupported(ei.value)


def test_normalize_tradingview_symbol_other_paths_unchanged():
    assert v.normalize_tradingview_symbol("BINANCE:BTCUSDT", "kucoin") == "BINANCE:BTCUSDT"
    assert v.normalize_tradingview_symbol("btcusdt", "kucoin") == "KUCOIN:BTCUSDT"
    assert v.normalize_tradingview_symbol("XAUUSD", "kucoin") == "TVC:GOLD"
    # A bare coin that happens to be named like the venue is not a prefix.
    assert v.normalize_tradingview_symbol("HYPERLIQUID", "kucoin") == "KUCOIN:HYPERLIQUID"


# ── screener_service: pick_fallback_exchange ────────────────────────────────

def test_fallback_filters_native_before_cap(monkeypatch):
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing(
        {"ASX", "BIST", "BURSA", "EGX", "HKEX", "HYPERLIQUID", "KUCOIN"}))
    assert ss.pick_fallback_exchange("X", "BINANCE") == "KUCOIN"


def test_fallback_native_only_returns_none(monkeypatch):
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing({"HYPERLIQUID"}))
    assert ss.pick_fallback_exchange("KPEPE", "BINANCE") is None


def test_fallback_requests_wide_scan(monkeypatch):
    seen = {}

    def fake(symbol, max_results=6):
        seen["max_results"] = max_results
        return ["BINANCE", "HYPERLIQUID"]

    monkeypatch.setattr(ss, "exchanges_listing_symbol", fake)
    assert ss.pick_fallback_exchange("BTC", "BINANCE") is None
    assert seen["max_results"] == ss._LISTING_SCAN_MAX


def test_fallback_still_caps_at_six(monkeypatch):
    # Seven TV venues: the 7th (alphabetically) is outside the historical cap.
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing(
        {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "MEXC"}))
    assert ss.pick_fallback_exchange("X", "AAA") == "BBB"


# ── screener_service: symbol_not_found_error ────────────────────────────────

def test_not_found_native_only_skips_lookup(monkeypatch):
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing({"HYPERLIQUID"}))
    monkeypatch.setattr(ss, "lookup_tradingview_instrument", _lookup_tripwire)
    err = ss.symbol_not_found_error("KPEPE", "BINANCE")["error"]
    assert err["code"] == "SYMBOL_NOT_FOUND"
    assert err["listed_on"] == []
    assert err["native_listed_on"] == ["HYPERLIQUID"]
    assert err["native_supported_tools"] == HL_TOOLS
    assert "verify the ticker" not in err["message"]
    assert "HYPERLIQUID also lists it, but " + v.native_support_hint("HYPERLIQUID") in err["message"]
    assert 'Do not retry this tool with exchange="HYPERLIQUID".' in err["message"]
    assert "Retry with one of those" not in err["message"]


def test_not_found_mixed_listing(monkeypatch):
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing(
        {"ASX", "HYPERLIQUID", "NASDAQ"}))
    monkeypatch.setattr(ss, "lookup_tradingview_instrument", _lookup_tripwire)
    err = ss.symbol_not_found_error("SOL", "NYSE")["error"]
    assert err["listed_on"] == ["ASX", "NASDAQ"]
    assert err["native_listed_on"] == ["HYPERLIQUID"]
    assert err["native_supported_tools"] == HL_TOOLS
    msg = err["message"]
    assert "the symbol is listed on: ASX, NASDAQ. Retry with one of those as `exchange`." in msg
    assert msg.index("Retry with one of those") < msg.index("HYPERLIQUID also lists it")


def test_not_found_tv_only_has_no_native_keys(monkeypatch):
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing({"BINANCE", "KUCOIN"}))
    monkeypatch.setattr(ss, "lookup_tradingview_instrument", _lookup_tripwire)
    err = ss.symbol_not_found_error("FOO", "MEXC")["error"]
    assert err["listed_on"] == ["BINANCE", "KUCOIN"]
    assert "native_listed_on" not in err
    assert "native_supported_tools" not in err


def test_not_found_listed_on_capped_at_six_after_filter(monkeypatch):
    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing(
        {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HYPERLIQUID"}))
    monkeypatch.setattr(ss, "lookup_tradingview_instrument", _lookup_tripwire)
    err = ss.symbol_not_found_error("FOO", "MEXC")["error"]
    assert err["listed_on"] == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    assert err["native_listed_on"] == ["HYPERLIQUID"]


def test_not_found_nothing_listed_keeps_lookup(monkeypatch):
    calls = []

    def fake_lookup(symbol, exchange=""):
        calls.append((symbol, exchange))
        return None

    monkeypatch.setattr(ss, "exchanges_listing_symbol", _fake_listing(set()))
    monkeypatch.setattr(ss, "lookup_tradingview_instrument", fake_lookup)
    err = ss.symbol_not_found_error("ZZZQ", "BINANCE")["error"]
    assert calls == [("ZZZQ", "BINANCE")]
    assert err["listed_on"] == []
    assert "verify the ticker" in err["message"]
    assert "native_listed_on" not in err


# ── validators.py: reject_native_symbol_prefix ──────────────────────────────

@pytest.mark.parametrize("sym", ["HYPERLIQUID:BTC", "hyperliquid:btc", " HyperLiquid:kPEPE "])
def test_reject_native_symbol_prefix_raises(sym):
    with pytest.raises(ScreenerServiceError) as ei:
        v.reject_native_symbol_prefix(sym)
    _assert_unsupported(ei.value)


@pytest.mark.parametrize("sym", [None, "", "COMI", "EGX:COMI", "HYPERLIQUID", "BINANCE:BTCUSDT"])
def test_reject_native_symbol_prefix_passes_others(sym):
    assert v.reject_native_symbol_prefix(sym) is None


def test_unsupported_error_tolerates_missing_tool_entry(monkeypatch):
    # A native venue without a NATIVE_PROVIDER_TOOLS entry must not KeyError.
    monkeypatch.setattr(v, "NATIVE_PROVIDERS", frozenset({"hyperliquid", "othervenue"}))
    assert v.native_support_hint("othervenue").startswith("OTHERVENUE is a native")
    with pytest.raises(ScreenerServiceError) as ei:
        v.validate_exchange("othervenue")
    assert ei.value.code == ErrorCode.UNSUPPORTED_EXCHANGE
    assert ei.value.extra["supported_tools"] == []


# ── server.py: EGX tools build full_symbol themselves (R4 gap) ──────────────

@pytest.fixture
def egx_calls(monkeypatch):
    """Record TradingView calls made by egx_service; return no data."""
    from tradingview_mcp.core.services import egx_service

    calls = {"analysis": [], "scan": []}

    def fake_analysis(screener, interval, symbols):
        calls["analysis"].append(list(symbols))
        return {}

    def fake_scan(*a, **kw):
        calls["scan"].append(kw.get("cache_key"))
        raise RuntimeError("no scan in tests")

    monkeypatch.setattr(egx_service, "_TA_AVAILABLE", True)
    monkeypatch.setattr(egx_service, "get_multiple_analysis", fake_analysis, raising=False)
    monkeypatch.setattr(egx_service, "_scan_with_retry", fake_scan)
    return calls


def _egx_tools():
    from tradingview_mcp import server
    return {
        "egx_trade_plan": lambda s: server.egx_trade_plan(s),
        "egx_fibonacci_retracement": lambda s: server.egx_fibonacci_retracement(s),
    }


@pytest.mark.parametrize("tool", ["egx_trade_plan", "egx_fibonacci_retracement"])
@pytest.mark.parametrize("sym", ["HYPERLIQUID:BTC", "hyperliquid:btc"])
def test_egx_tools_reject_native_prefix(egx_calls, tool, sym):
    out = _egx_tools()[tool](sym)
    assert out["error"]["code"] == "UNSUPPORTED_EXCHANGE"
    assert out["error"]["exchange"] == "HYPERLIQUID"
    assert out["error"]["supported_tools"] == HL_TOOLS
    assert egx_calls["analysis"] == []
    assert egx_calls["scan"] == []


@pytest.mark.parametrize("tool", ["egx_trade_plan", "egx_fibonacci_retracement"])
def test_egx_tools_still_reach_tradingview_for_egx_symbols(egx_calls, tool):
    _egx_tools()[tool]("COMI")
    assert egx_calls["analysis"] == [["EGX:COMI"]]
