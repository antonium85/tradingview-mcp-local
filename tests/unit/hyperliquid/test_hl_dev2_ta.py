"""WP-TA: Hyperliquid analysis adapter (candles -> TradingView-shaped TA)."""
from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_analysis as ta
from tradingview_mcp.core.services import hyperliquid_service as hl_svc
from tradingview_mcp.core.services import indicators_calc as calc


def _candles(n, base=100.0, drift=0.5, step_ms=3_600_000):
    out = []
    for i in range(n):
        c = base + drift * i + math.sin(i / 3) * 2
        out.append({"t": 1_700_000_000_000 + i * step_ms, "open": c - 0.3, "high": c + 1.0,
                    "low": c - 1.0, "close": c, "volume": 10.0 + (i % 7)})
    return out


def _flatten(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _flatten(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten(v)


def test_indicator_snapshot_values_and_keys():
    cs = _candles(300)
    s = ta.indicator_snapshot(cs)
    closes = [c["close"] for c in cs]
    assert s["close"] == cs[-1]["close"]
    assert s["RSI"] == calc.calc_rsi(closes, 14)[-1]
    assert s["EMA200"] == calc.calc_ema(closes, 200)[-1]
    assert s["SMA50"] == calc.calc_sma(closes, 50)[-1]
    assert s["BB.upper"] == calc.calc_bollinger(closes, 20, 2.0)["upper"][-1]
    assert s["Supertrend.dir"] in (1, -1)
    for k in ("MACD.macd", "MACD.signal", "EMA9", "EMA10", "SMA10", "SMA200", "ATR",
              "volume.SMA20", "BB.lower", "Supertrend.upper", "Supertrend.lower"):
        assert k in s


def test_indicator_snapshot_short_history_gives_none():
    s = ta.indicator_snapshot(_candles(5))
    assert s["EMA200"] is None and s["RSI"] is None and s["BB.upper"] is None
    assert s["close"] is not None


def test_indicator_snapshot_empty_raises_no_data():
    with pytest.raises(ScreenerServiceError) as ei:
        ta.indicator_snapshot([])
    assert ei.value.code == ErrorCode.NO_DATA


MARKET = {"coin": "kPEPE", "mark_price": 0.0042, "prev_day_price": 0.004,
          "change_pct_24h": 5.0, "funding_rate_1h": 1e-5, "funding_annualized_pct": 8.76,
          "open_interest_usd": 1e6, "volume_24h_usd": 2e6, "extra": 1}


def test_build_coin_analysis_shape_and_no_tv_rating():
    cs = _candles(300)
    res = {"coin": "kPEPE", "interval": "1h", "candles": cs}
    out = ta.build_coin_analysis(MARKET, "1h", res)
    assert out["symbol"] == "HYPERLIQUID:kPEPE" and out["source"] == "hyperliquid"
    assert out["bars_used"] == 300 and out["interval"] == "1h"
    assert out["last_bar_time"].endswith("Z") and out["last_bar_time"].startswith("2023-")
    assert set(out["perp_context"]) == set(ta._PERP_KEYS)
    keys = set(_flatten(out))
    assert "Recommend.All" not in keys and "tv_recommendation" not in keys
    assert "Recommend.All" in out["not_available"]
    assert out["market_structure"].keys() == {"trend", "trend_score"}
    assert out["timeframe_context"]["timeframe"] == "1h"
    assert out["market_sentiment"]["buy_sell_signal"] in ("BUY", "SELL", "NEUTRAL")


def test_sub_cent_prices_keep_six_significant_digits():
    cs = _candles(300, base=100.0, drift=0.5)
    for c in cs:
        for k in ("open", "high", "low", "close"):
            c[k] = c[k] * 1.23456789e-5
    out = ta.build_coin_analysis(MARKET, "1h", {"interval": "1h", "candles": cs})
    price = out["price_data"]["current_price"]
    assert 0 < price < 1
    assert price == float(f"{cs[-1]['close']:.6g}")
    assert price != round(cs[-1]["close"], 4)
    assert out["ema"]["ema20"] == float(f"{ta.indicator_snapshot(cs)['EMA20']:.6g}")


def test_short_history_sections_degrade():
    out = ta.build_coin_analysis(MARKET, "1h", {"interval": "1h", "candles": _candles(3)})
    assert out["rsi"]["value"] is None and out["rsi"]["signal"] == "Neutral"
    assert out["bollinger_bands"]["position"] == "Unknown"
    assert out["bollinger_bands"]["squeeze"] is False
    assert out["market_sentiment"]["overall_rating"] is None
    assert out["market_sentiment"]["buy_sell_signal"] == "N/A"
    assert out["market_sentiment"]["volatility"] == "Unknown"
    assert out["atr"]["volatility"] == "Unknown"
    assert out["supertrend"]["direction"] is None


def test_build_tf_entry_keys():
    entry, bias = ta.build_tf_entry("1D", _candles(300))
    assert bias in (-1, 0, 1)
    assert set(entry) == {"label", "bias", "bias_reasons", "key_indicators", "advice", "price",
                          "change_pct", "rsi", "macd_crossover", "ema_trend", "volume_signal",
                          "market_structure", "supertrend", "bars_used"}
    assert entry["label"] == ta.MTF_LABELS["1D"] and entry["bars_used"] == 300


@pytest.mark.parametrize("scores,status,conf", [
    ([("1W", 1), ("1D", 1)], "FULLY ALIGNED BULLISH", "Very High"),
    ([("1W", -1), ("1D", -1)], "FULLY ALIGNED BEARISH", "Very High"),
    ([("1W", 1), ("1D", 1), ("4h", 1), ("1h", 1), ("15m", 0)], "MOSTLY BULLISH", "High"),
    ([("1W", 1), ("1D", 0), ("4h", 0)], "LEAN BULLISH", "Medium"),
    ([("1W", 1), ("1D", -1)], "MIXED/RANGING", "Low"),
    ([], "MIXED/RANGING", "Low"),
])
def test_build_alignment(scores, status, conf):
    al, rec = ta.build_alignment(scores)
    assert al["status"] == status and al["confidence"] == conf
    assert al["scores_by_tf"] == dict(scores)
    assert len(rec["rules"]) == 5
    assert rec["entry_timeframe"] == ("1H or 4H pullback" if al["net_score"] > 0
                                      else "Wait for alignment")


def test_build_alignment_divergent():
    al, _ = ta.build_alignment([("1W", 1), ("1D", -1), ("4h", 0), ("1h", 1)])
    assert al["divergent_timeframes"] == ["1D"]


def test_sync_analysis_uses_sync_client_only(hl_mock, monkeypatch):
    def boom():
        raise AssertionError("async client used from sync path")
    monkeypatch.setattr(hl_svc, "get_async_client", boom)
    out = ta.analyze_hyperliquid_coin("btcusdt", "1h")
    assert out["symbol"] == "HYPERLIQUID:BTC" and out["bars_used"] > 0
    assert len(hl_mock.of_type("candleSnapshot")) == 1
    assert hl_mock.of_type("candleSnapshot")[0]["req"]["interval"] == "1h"


def test_sync_unknown_symbol_no_candle_request(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        ta.analyze_hyperliquid_coin("NOSUCHCOINXYZ", "1h")
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND
    assert hl_mock.of_type("candleSnapshot") == []


def test_sync_empty_candles_no_data(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(200, json=[])])
    with pytest.raises(ScreenerServiceError) as ei:
        ta.analyze_hyperliquid_coin("BTC", "1h")
    assert ei.value.code == ErrorCode.NO_DATA


def test_async_mtf_requests_five_intervals_once(hl_mock, monkeypatch):
    def boom():
        raise AssertionError("sync client used from async path")
    monkeypatch.setattr(hl_svc, "get_client", boom)
    out = asyncio.run(ta.analyze_hyperliquid_mtf("BTC-PERP"))
    ivs = sorted(b["req"]["interval"] for b in hl_mock.of_type("candleSnapshot"))
    assert ivs == sorted(["1w", "1d", "4h", "1h", "15m"])
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1
    assert list(out["timeframes"]) == list(ta.MTF_TIMEFRAMES)
    assert out["symbol"] == "HYPERLIQUID:BTC" and out["source"] == "hyperliquid"
    assert out["alignment"]["scores_by_tf"].keys() <= set(ta.MTF_TIMEFRAMES)
    assert "Recommend.All" not in set(_flatten(out))


def test_async_mtf_all_timeframes_failing_raises_typed_error(hl_mock):
    def fail_4h(request):
        import json
        if json.loads(request.content)["req"]["interval"] == "4h":
            return httpx.Response(400, text="bad")
        return httpx.Response(200, json=[])
    hl_mock.script("candleSnapshot", [fail_4h])
    # everything empty/failing -> typed error
    with pytest.raises(ScreenerServiceError):
        asyncio.run(ta.analyze_hyperliquid_mtf("BTC"))


def test_async_mtf_one_empty_timeframe_is_error_entry(hl_mock):
    import json
    import pathlib
    fx = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"
    data = json.loads((fx / "candle_snapshot_BTC_1h.json").read_text())

    def route(request):
        if json.loads(request.content)["req"]["interval"] == "15m":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=data)

    hl_mock.script("candleSnapshot", [route])
    out = asyncio.run(ta.analyze_hyperliquid_mtf("BTC"))
    assert out["timeframes"]["15m"] == {"error": "No data for 15m"}
    assert "15m" not in out["alignment"]["scores_by_tf"]
    assert out["timeframes"]["1D"]["label"] == ta.MTF_LABELS["1D"]


def test_async_mtf_unknown_symbol_raises_before_candles(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        asyncio.run(ta.analyze_hyperliquid_mtf("NOSUCHCOINXYZ"))
    assert ei.value.code == ErrorCode.SYMBOL_NOT_FOUND
    assert hl_mock.of_type("candleSnapshot") == []
