"""Hyperliquid coin_analysis / multi_timeframe_analysis adapter (spec step-2 sections 2.3 and 5.4)."""
from __future__ import annotations

import importlib
import json
import pathlib

import httpx
import pytest

from tradingview_mcp.core.errors import ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_service as svc
from tradingview_mcp.core.services import indicators as ind
from tradingview_mcp.core.services import indicators_calc as calc

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"
TFS = ["1W", "1D", "4h", "1h", "15m"]
INTERVAL_FIXTURE = {
    "1d": "candle_snapshot_BTC_1d.json", "4h": "candle_snapshot_BTC_4h.json",
    "15m": "candle_snapshot_BTC_15m.json", "1w": "candle_snapshot_BTC_1w.json",
}

SNAPSHOT_KEYS = {
    "open", "high", "low", "close", "volume", "RSI", "MACD.macd", "MACD.signal",
    "EMA9", "EMA10", "EMA20", "EMA30", "EMA50", "EMA100", "EMA200",
    "SMA10", "SMA20", "SMA30", "SMA50", "SMA100", "SMA200",
    "BB.upper", "BB.lower", "ATR", "volume.SMA20",
    "Supertrend.dir", "Supertrend.upper", "Supertrend.lower",
}
COIN_KEYS = {
    "symbol", "coin", "exchange", "source", "timeframe", "interval", "bars_used", "last_bar_time",
    "price_data", "timeframe_context", "rsi", "macd", "sma", "ema", "bollinger_bands", "atr",
    "volume_analysis", "supertrend", "market_structure", "market_sentiment", "perp_context",
    "not_available", "note",
}
ENTRY_KEYS = {
    "label", "bias", "bias_reasons", "key_indicators", "advice", "price", "change_pct", "rsi",
    "macd_crossover", "ema_trend", "volume_signal", "market_structure", "supertrend", "bars_used",
}
PERP_KEYS = ("mark_price", "prev_day_price", "change_pct_24h", "funding_rate_1h",
             "funding_annualized_pct", "open_interest_usd", "volume_24h_usd")


def an():
    return importlib.import_module("tradingview_mcp.core.services.hyperliquid_analysis")


def fx(name):
    return json.loads((FIXTURES / name).read_text())


def candles_of(name):
    return svc.parse_candles(fx(name))


def keys_recursive(obj):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= keys_recursive(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= keys_recursive(v)
    return out


@pytest.fixture(autouse=True)
def _no_net(no_network):
    yield
    assert no_network == []


@pytest.fixture
def btc_market():
    markets, _ = svc.parse_markets(fx("meta_and_asset_ctxs.json"))
    return next(m for m in markets if m["coin"] == "BTC")


def synthetic(n):
    return [{"t": 1_700_000_000_000 + i * 3_600_000, "close_time": 0, "open": 100.0 + i,
             "high": 102.0 + i, "low": 99.0 + i, "close": 101.0 + i, "volume": 10.0 + i, "trades": 1}
            for i in range(n)]


# ── indicator_snapshot ───────────────────────────────────────────────────────

def test_snapshot_keys_and_values_btc_1h():
    candles = candles_of("candle_snapshot_BTC_1h.json")
    s = an().indicator_snapshot(candles)
    assert SNAPSHOT_KEYS <= set(s)
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    assert s["close"] == closes[-1]
    assert s["open"] == candles[-1]["open"]
    assert s["volume"] == candles[-1]["volume"]
    assert s["RSI"] == calc.calc_rsi(closes, 14)[-1]
    assert s["EMA20"] == calc.calc_ema(closes, 20)[-1]
    assert s["SMA50"] == calc.calc_sma(closes, 50)[-1]
    assert s["MACD.macd"] == calc.calc_macd(closes, 12, 26, 9)["macd"][-1]
    assert s["BB.upper"] == calc.calc_bollinger(closes, 20, 2.0)["upper"][-1]
    assert s["ATR"] == calc.calc_atr(highs, lows, closes, 14)[-1]
    assert s["Supertrend.dir"] in (1, -1, None)


def test_snapshot_short_history_gives_none():
    s = an().indicator_snapshot(synthetic(10))
    assert SNAPSHOT_KEYS <= set(s)
    for k in ("SMA200", "EMA200", "RSI", "MACD.signal"):
        assert s[k] is None, k


def test_snapshot_empty_raises_no_data():
    with pytest.raises(ScreenerServiceError) as ei:
        an().indicator_snapshot([])
    assert ei.value.to_envelope()["error"]["code"] == "NO_DATA"


# ── coin analysis (service level) ────────────────────────────────────────────

def test_coin_analysis_btc_1h(btc_market, hl_mock, monkeypatch):
    monkeypatch.setattr(svc, "get_async_client", lambda: (_ for _ in ()).throw(AssertionError("async client")))
    res = an().analyze_hyperliquid_coin("BTC", "1h")
    candles = candles_of("candle_snapshot_BTC_1h.json")
    closes = [c["close"] for c in candles]
    assert set(res) == COIN_KEYS
    assert res["symbol"] == "HYPERLIQUID:BTC" and res["coin"] == "BTC"
    assert res["exchange"] == "HYPERLIQUID" and res["source"] == "hyperliquid"
    assert res["timeframe"] == "1h" and res["interval"] == "1h"
    assert res["bars_used"] == len(candles)
    assert res["rsi"]["value"] == round(calc.calc_rsi(closes, 14)[-1], 2)
    assert res["perp_context"] == {k: btc_market[k] for k in PERP_KEYS}
    assert "Recommend.All" in res["not_available"]
    found = keys_recursive(res)
    assert "Recommend.All" not in found and "tv_recommendation" not in found
    bb_u = calc.calc_bollinger(closes, 20, 2.0)
    want = ind.compute_bb_rating_signal(
        closes[-1], bb_u["upper"][-1], bb_u["middle"][-1], bb_u["lower"][-1])
    assert res["market_sentiment"]["overall_rating"] == want[0]
    assert res["market_sentiment"]["buy_sell_signal"] == want[1]
    assert res["market_sentiment"]["momentum"] in ("Bullish", "Bearish")
    assert res["market_sentiment"]["volatility"] in ("Unknown", "High", "Medium", "Low")
    assert res["last_bar_time"].endswith("Z") and len(res["last_bar_time"]) == 20
    pd = res["price_data"]
    assert pd["close"] == pd["current_price"]
    assert set(pd) == {"current_price", "open", "high", "low", "close", "change_percent", "volume"}
    assert set(res["macd"]) == {"macd_line", "signal_line", "histogram", "crossover"}
    assert set(res["sma"]) == {"sma10", "sma20", "sma30", "sma50", "sma100", "sma200"}
    assert set(res["ema"]) == {"ema9", "ema10", "ema20", "ema30", "ema50", "ema100", "ema200"}
    assert {"upper", "middle", "lower", "width", "squeeze", "position"} <= set(res["bollinger_bands"])
    assert set(res["atr"]) == {"value", "percent_of_price", "volatility"}
    assert set(res["volume_analysis"]) == {"current", "average_20", "ratio", "signal"}
    assert set(res["supertrend"]) == {"direction", "upper", "lower"}
    assert set(res["market_structure"]) == {"trend", "trend_score"}


def test_coin_analysis_request_window(hl_mock, frozen_now):
    an().analyze_hyperliquid_coin("BTC", "1h")
    reqs = hl_mock.of_type("candleSnapshot")
    assert len(reqs) == 1
    r = reqs[0]["req"]
    assert r["coin"] == "BTC" and r["interval"] == "1h"
    assert r["endTime"] - r["startTime"] == 300 * 3_600_000


def test_coin_analysis_sub_dollar_keeps_six_sig_digits(hl_mock):
    res = an().analyze_hyperliquid_coin("KPEPE", "1D")
    candles = candles_of("candle_snapshot_kPEPE_1d.json")
    closes = [c["close"] for c in candles]
    assert closes[-1] < 1
    assert res["coin"] == "kPEPE" and res["symbol"] == "HYPERLIQUID:kPEPE"
    req = hl_mock.of_type("candleSnapshot")[0]["req"]
    assert req["coin"] == "kPEPE" and req["interval"] == "1d"
    assert res["price_data"]["close"] == float(f"{closes[-1]:.6g}")
    assert res["ema"]["ema20"] == float(f"{calc.calc_ema(closes, 20)[-1]:.6g}")
    assert res["sma"]["sma200"] is None


def test_coin_analysis_unknown_symbol(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        an().analyze_hyperliquid_coin("NOTACOIN", "1h")
    assert ei.value.to_envelope()["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert hl_mock.of_type("candleSnapshot") == []


def test_coin_analysis_delisted(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        an().analyze_hyperliquid_coin("MATIC", "1h")
    err = ei.value.to_envelope()["error"]
    assert err["code"] == "SYMBOL_NOT_FOUND" and err["delisted"] is True
    assert hl_mock.of_type("candleSnapshot") == []


def test_coin_analysis_empty_candles(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(200, json=[])])
    with pytest.raises(ScreenerServiceError) as ei:
        an().analyze_hyperliquid_coin("BTC", "1h")
    err = ei.value.to_envelope()["error"]
    assert err["code"] == "NO_DATA" and err["exchange"] == "HYPERLIQUID" and err["retryable"] is False


def test_coin_analysis_persistent_500(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(500, text="boom")])
    with pytest.raises(ScreenerServiceError) as ei:
        an().analyze_hyperliquid_coin("BTC", "1h")
    err = ei.value.to_envelope()["error"]
    assert err["code"] == "UPSTREAM_ERROR" and err["retryable"] is True


# ── build_tf_entry ───────────────────────────────────────────────────────────

def test_build_tf_entry_shape():
    candles = candles_of("candle_snapshot_BTC_4h.json")
    entry, bias = an().build_tf_entry("4h", candles)
    assert set(entry) == ENTRY_KEYS
    assert bias in (1, -1, 0)
    assert entry["label"] == "4-Hour (Refinement)"
    assert entry["bars_used"] == len(candles)
    assert set(entry["ema_trend"]) == {"ema20", "ema50", "ema200"}
    assert entry["supertrend"] in ("Bullish", "Bearish", None)


def test_labels_and_constants():
    a = an()
    assert a.TA_BARS == 300
    assert tuple(a.MTF_TIMEFRAMES) == tuple(TFS)
    assert a.MTF_LABELS == {
        "1W": "Weekly (Trend Bias)", "1D": "Daily (Swing Setup)", "4h": "4-Hour (Refinement)",
        "1h": "1-Hour (Entry Timing)", "15m": "15-Min (Execution)"}
    assert "Recommend.All" in a.NOT_AVAILABLE


# ── build_alignment ──────────────────────────────────────────────────────────

def test_alignment_fully_bullish_and_bearish():
    a, rec = an().build_alignment([(t, 1) for t in TFS])
    assert a["status"] == "FULLY ALIGNED BULLISH"
    assert set(a) == {"status", "confidence", "net_score", "scores_by_tf", "divergent_timeframes"}
    assert set(rec) == {"action", "entry_timeframe", "rules"}
    assert len(rec["rules"]) == 5
    a, _ = an().build_alignment([(t, -1) for t in TFS])
    assert a["status"] == "FULLY ALIGNED BEARISH"


def test_alignment_mostly_bullish():
    a, rec = an().build_alignment(list(zip(TFS, [1, 1, 1, 0, 0])))
    assert a["status"] == "MOSTLY BULLISH"
    assert a["net_score"] == 3
    assert len(rec["rules"]) == 5


def test_alignment_lean_bullish_with_divergence():
    scores = [("1W", 1), ("1D", 1), ("4h", 1), ("1h", -1), ("15m", 0)]
    a, _ = an().build_alignment(scores)
    assert a["status"] == "LEAN BULLISH"
    assert a["divergent_timeframes"] == ["1h"]
    assert a["scores_by_tf"] == dict(scores)


def test_alignment_empty():
    a, rec = an().build_alignment([])
    assert a["status"] == "MIXED/RANGING" and a["confidence"] == "Low"
    assert len(rec["rules"]) == 5


# ── multi-timeframe (service level) ──────────────────────────────────────────

async def test_mtf_btc(hl_mock, monkeypatch):
    def boom():
        raise AssertionError("sync client used from async path")
    monkeypatch.setattr(svc, "get_client", boom)
    res = await an().analyze_hyperliquid_mtf("BTC")
    assert list(res["timeframes"]) == TFS
    for tf, e in res["timeframes"].items():
        assert set(e) == ENTRY_KEYS, tf
    reqs = hl_mock.of_type("candleSnapshot")
    assert sorted(r["req"]["interval"] for r in reqs) == sorted(["1w", "1d", "4h", "1h", "15m"])
    assert {r["req"]["coin"] for r in reqs} == {"BTC"}
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1
    assert set(res["alignment"]["scores_by_tf"]) <= set(TFS)
    assert res["source"] == "hyperliquid" and res["exchange"] == "HYPERLIQUID"
    assert res["symbol"] == "HYPERLIQUID:BTC" and res["coin"] == "BTC"
    assert res["analysis_type"] == "Multi-Timeframe Alignment"
    assert {"alignment", "recommendation", "not_available"} <= set(res)
    found = keys_recursive(res)
    assert "Recommend.All" not in found and "tv_recommendation" not in found


async def test_mtf_partial_failure(hl_mock):
    def respond(request):
        body = json.loads(request.content)
        iv = body["req"]["interval"]
        if iv == "4h":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=fx(INTERVAL_FIXTURE.get(iv, "candle_snapshot_BTC_1h.json")))

    hl_mock.script("candleSnapshot", [respond])
    res = await an().analyze_hyperliquid_mtf("BTC")
    assert "error" in res["timeframes"]["4h"]
    for tf in ("1W", "1D", "1h", "15m"):
        assert set(res["timeframes"][tf]) == ENTRY_KEYS
    assert "4h" not in res["alignment"]["scores_by_tf"]


async def test_mtf_total_failure(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(500, text="boom")])
    with pytest.raises(ScreenerServiceError) as ei:
        await an().analyze_hyperliquid_mtf("BTC")
    assert ei.value.to_envelope()["error"]["code"] == "UPSTREAM_ERROR"


async def test_mtf_all_empty_is_no_data(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(200, json=[])])
    with pytest.raises(ScreenerServiceError) as ei:
        await an().analyze_hyperliquid_mtf("BTC")
    assert ei.value.to_envelope()["error"]["code"] == "NO_DATA"


async def test_mtf_unknown_symbol(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        await an().analyze_hyperliquid_mtf("NOTACOIN")
    assert ei.value.to_envelope()["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert hl_mock.of_type("candleSnapshot") == []
