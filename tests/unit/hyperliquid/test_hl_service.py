"""Hyperliquid provider (spec step-1 section 5.4). Real fixtures; no network."""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import inspect
import json
import math
import pathlib

import httpx
import pytest

from tradingview_mcp.core.errors import ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_service as svc

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"
NOW = 1_790_852_412_345  # arbitrary fixed wall clock (ms)
NOW_FLOOR = NOW // 60_000 * 60_000
DAY = 86_400_000
HOUR = 3_600_000
PUBLIC = ["post_info", "market_snapshot", "resolve_coin", "get_market", "candles", "funding_history", "l2_book"]


def fx(name):
    return json.loads((FIXTURES / name).read_text())


def err_of(exc_info) -> dict:
    return exc_info.value.to_envelope()["error"]


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(svc, "_now_ms", lambda: NOW)


@pytest.fixture
def meta():
    return fx("meta_and_asset_ctxs.json")


# ── mapping and windows ──────────────────────────────────────────────────────

@pytest.mark.parametrize("tf,expected", [
    ("5m", "5m"), ("15m", "15m"), ("1h", "1h"), ("4h", "4h"),
    ("1D", "1d"), ("1d", "1d"), ("1W", "1w"), ("1w", "1w"), ("1M", "1M"), (" 4H ", "4h"),
])
def test_map_timeframe(tf, expected):
    assert svc.map_timeframe(tf) == expected


@pytest.mark.parametrize("tf", ["30m", ""])
def test_map_timeframe_rejects(tf):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.map_timeframe(tf)
    e = err_of(ei)
    assert e["code"] == "INVALID_TIMEFRAME"
    assert e["valid_timeframes"]


def test_constants():
    assert svc.HL_MAX_CANDLES == 5000
    assert svc.HL_MAX_FUNDING_ROWS == 500
    assert svc.HL_MAX_BOOK_LEVELS == 20
    assert svc.DEFAULT_CANDLE_BARS == 300
    assert svc.SOURCE == "hyperliquid" and svc.EXCHANGE_LABEL == "HYPERLIQUID"
    assert svc.INTERVAL_MS["1M"] == 2_592_000_000 and svc.interval_ms("1h") == HOUR


@pytest.mark.parametrize("period,days", [("1mo", 30), ("3mo", 90), ("6mo", 180), ("1y", 365), ("2y", 730)])
def test_period_to_window(period, days):
    start, end = svc.period_to_window_ms(period, now_ms=NOW)
    assert end == NOW_FLOOR
    assert end - start == days * DAY


def test_period_is_stripped_and_lowercased():
    assert svc.period_to_window_ms(" 1Y ", now_ms=NOW) == svc.period_to_window_ms("1y", now_ms=NOW)


def test_period_unknown():
    with pytest.raises(ScreenerServiceError) as ei:
        svc.period_to_window_ms("5y", now_ms=NOW)
    e = err_of(ei)
    assert e["code"] == "INVALID_PARAMETER"
    assert e["valid_periods"] == sorted(svc.PERIOD_DAYS)


def test_requested_bar_count():
    assert svc.requested_bar_count(0, 365 * DAY, "1h") == 8760
    assert svc.requested_bar_count(0, 1, "1h") == 1
    assert svc.requested_bar_count(0, HOUR + 1, "1h") == 2
    assert svc.requested_bar_count(10, 10, "1h") == 1


def test_weights():
    assert svc.request_weight({"type": "l2Book", "coin": "BTC"}) == 2
    for t in ("metaAndAssetCtxs", "candleSnapshot", "fundingHistory"):
        assert svc.request_weight({"type": t}) == 20
    assert svc.response_extra_weight({"type": "candleSnapshot"}, 120) == 2
    assert svc.response_extra_weight({"type": "fundingHistory"}, 45) == 2
    assert svc.response_extra_weight({"type": "l2Book"}, 500) == 0
    assert svc.response_extra_weight({"type": "metaAndAssetCtxs"}, 500) == 0


# ── parsers ──────────────────────────────────────────────────────────────────

MARKET_KEYS = {
    "coin", "mark_price", "prev_day_price", "change_pct_24h", "volume_24h_usd", "volume_24h_base",
    "open_interest", "open_interest_usd", "funding_rate_1h", "funding_annualized_pct", "premium",
    "oracle_price", "mid_price", "max_leverage", "only_isolated",
}


def test_parse_markets_delisted_split(meta):
    markets, delisted = svc.parse_markets(meta)
    names = [m["coin"] for m in markets]
    assert "MATIC" in delisted and "RNDR" in delisted
    assert "MATIC" not in names and "RNDR" not in names
    universe = meta[0]["universe"]
    n_delisted = sum(1 for u in universe if u.get("isDelisted"))
    assert len(markets) == len(universe) - n_delisted
    assert len(delisted) == n_delisted
    live_in_order = [u["name"] for u in universe if not u.get("isDelisted")]
    assert names == live_in_order
    assert {"kPEPE", "kBONK", "kSHIB"} <= set(names)


def test_parse_markets_values_follow_formulas(meta):
    markets, _ = svc.parse_markets(meta)
    by_name = {m["coin"]: m for m in markets}
    for u, c in zip(meta[0]["universe"], meta[1]):
        if u.get("isDelisted"):
            continue
        m = by_name[u["name"]]
        assert set(m) == MARKET_KEYS
        mark = float(c["markPx"])
        prev = float(c["prevDayPx"]) if c.get("prevDayPx") is not None else None
        assert m["mark_price"] == mark
        assert m["volume_24h_usd"] == float(c["dayNtlVlm"])
        assert m["volume_24h_base"] == float(c["dayBaseVlm"])
        assert m["open_interest"] == float(c["openInterest"])
        assert m["open_interest_usd"] == pytest.approx(round(float(c["openInterest"]) * mark, 2))
        assert m["funding_rate_1h"] == float(c["funding"])
        assert m["funding_annualized_pct"] == pytest.approx(round(float(c["funding"]) * 24 * 365 * 100, 4))
        if prev:
            assert m["change_pct_24h"] == pytest.approx(round((mark / prev - 1) * 100, 4))
        else:
            assert m["change_pct_24h"] is None
        assert isinstance(m["max_leverage"], int)
        assert isinstance(m["only_isolated"], bool)


def test_parse_markets_null_safe(meta):
    mod = copy.deepcopy(meta)
    first_live = next(i for i, u in enumerate(mod[0]["universe"]) if not u.get("isDelisted"))
    mod[1][first_live]["premium"] = None
    mod[1][first_live]["midPx"] = None
    mod[1][first_live]["prevDayPx"] = "0.0"
    markets, _ = svc.parse_markets(mod)
    m = markets[0]
    assert m["premium"] is None and m["mid_price"] is None
    assert m["change_pct_24h"] is None
    mod[1][first_live]["prevDayPx"] = None
    assert svc.parse_markets(mod)[0][0]["change_pct_24h"] is None


@pytest.mark.parametrize("bad", [None, {"x": 1}, [], [1, 2], "nope"])
def test_parse_markets_malformed(bad):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.parse_markets(bad)
    e = err_of(ei)
    assert e["code"] == "UPSTREAM_ERROR" and e["retryable"] is True


def test_rank_markets():
    ms = [
        {"coin": "A", "v": 5.0, "c": 1.0}, {"coin": "B", "v": 9.0, "c": None},
        {"coin": "C", "v": 7.0, "c": -3.0}, {"coin": "D", "v": 7.0, "c": 2.0},
        {"coin": "E", "v": None, "c": 0.5},
    ]
    assert [m["coin"] for m in svc.rank_markets(ms, "v")] == ["B", "C", "D", "A"]  # ties keep input order
    assert [m["coin"] for m in svc.rank_markets(ms, "v", descending=False)] == ["A", "C", "D", "B"]
    assert [m["coin"] for m in svc.rank_markets(ms, "c")] == ["D", "A", "E", "C"]
    assert [m["coin"] for m in svc.rank_markets(ms, "c", descending=False)] == ["C", "E", "A", "D"]
    assert [m["coin"] for m in svc.rank_markets(ms, "v", limit=2)] == ["B", "C"]


def test_rank_markets_on_fixture(meta):
    markets, _ = svc.parse_markets(meta)
    out = svc.rank_markets(markets, "volume_24h_usd")
    vols = [m["volume_24h_usd"] for m in out]
    assert vols == sorted(vols, reverse=True)
    assert len(out) == len(markets)


def test_parse_candles_sorted_dedup_typed():
    raw = fx("candle_snapshot_BTC_1h.json")
    scrambled = list(reversed(raw)) + [raw[5]]
    out = svc.parse_candles(scrambled)
    assert [c["t"] for c in out] == [r["t"] for r in raw]
    c0, r0 = out[0], raw[0]
    assert set(c0) == {"t", "close_time", "open", "high", "low", "close", "volume", "trades"}
    assert isinstance(c0["t"], int) and isinstance(c0["close_time"], int) and isinstance(c0["trades"], int)
    assert c0["close_time"] == r0["T"] and c0["trades"] == r0["n"]
    for k, rk in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v")):
        assert isinstance(c0[k], float) and c0[k] == float(r0[rk])


def test_parse_candles_empty():
    assert svc.parse_candles([]) == []


def _candle(t, o, h, l, c, v=1234.5678):
    return {"t": t, "close_time": t + 1, "open": o, "high": h, "low": l, "close": c, "volume": v, "trades": 1}


def test_to_backtest_candles_shape_and_dates():
    t = 1785974400000  # 2026-08-06 00:00:00 UTC
    t_h = t + 5 * HOUR + 30 * 60_000
    day = svc.to_backtest_candles([_candle(t, 1, 2, 0.5, 1.5)], "1d")[0]
    hour = svc.to_backtest_candles([_candle(t_h, 1, 2, 0.5, 1.5)], "1h")[0]
    assert set(day) == {"date", "open", "high", "low", "close", "volume"}
    assert day["date"] == dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
    assert hour["date"] == dt.datetime.fromtimestamp(t_h / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    assert svc.to_backtest_candles([_candle(t, 1, 2, 0.5, 1.5)], "1w")[0]["date"] == day["date"]
    assert svc.to_backtest_candles([_candle(t, 1, 2, 0.5, 1.5)], "1M")[0]["date"] == day["date"]
    assert len(svc.to_backtest_candles([_candle(t, 1, 2, 0.5, 1.5)], "4h")[0]["date"]) == len("2026-08-06 00:00")


def test_to_backtest_rounding_split():
    c = svc.to_backtest_candles([_candle(0, 85361.123456, 85361.123456, 85361.123456, 85361.123456, 1.23456789)], "1d")[0]
    assert c["open"] == 85361.1235
    assert c["volume"] == 1.23456789  # volume is not rounded
    k = svc.to_backtest_candles([_candle(0, 0.004283, 0.004283, 0.004283, 0.004283)], "1d")[0]
    assert k["close"] == 0.004283
    tiny = svc.to_backtest_candles([_candle(0, 0.000174123456, 1, 1, 1)], "1d")[0]
    assert tiny["open"] == float(f"{0.000174123456:.6g}")


def test_to_backtest_on_real_kpepe_fixture_keeps_precision():
    parsed = svc.parse_candles(fx("candle_snapshot_kPEPE_1d.json"))
    out = svc.to_backtest_candles(parsed, "1d")
    assert len(out) == len(parsed)
    for p, o in zip(parsed, out):
        if abs(p["close"]) < 1:
            assert o["close"] == float(f"{p['close']:.6g}")
            assert o["close"] != 0


def test_parse_l2_book():
    raw = fx("l2_book_BTC.json")
    book = svc.parse_l2_book(raw, 20)
    assert book["coin"] == "BTC" and isinstance(book["time_ms"], int)
    assert book["bids"][0]["price"] < book["asks"][0]["price"]
    assert set(book["bids"][0]) == {"price", "size", "orders"}
    assert isinstance(book["bids"][0]["orders"], int)
    bp = [x["price"] for x in book["bids"]]
    ap = [x["price"] for x in book["asks"]]
    assert bp == sorted(bp, reverse=True) and ap == sorted(ap)
    assert len(svc.parse_l2_book(raw, 5)["bids"]) == 5
    assert len(svc.parse_l2_book(raw, 5)["asks"]) == 5
    assert len(svc.parse_l2_book(raw, 1000)["bids"]) <= 20


def test_parse_l2_book_null_is_no_data():
    with pytest.raises(ScreenerServiceError) as ei:
        svc.parse_l2_book(fx("l2_book_unknown_null.json"), 10)
    e = err_of(ei)
    assert e["code"] == "NO_DATA" and e["retryable"] is False


def test_parse_funding_history():
    raw = fx("funding_history_BTC.json")
    rows = svc.parse_funding_history(list(reversed(raw)))
    assert [r["time_ms"] for r in rows] == sorted(r["time"] for r in raw)
    assert len(rows) == len(raw)
    r0 = rows[0]
    assert set(r0) == {"time_ms", "time", "funding_rate_1h", "funding_annualized_pct", "premium"}
    for r in rows:
        assert r["funding_annualized_pct"] == pytest.approx(round(r["funding_rate_1h"] * 8760 * 100, 4))
    assert r0["time"] == dt.datetime.fromtimestamp(r0["time_ms"] / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── clients: shapes and request bodies ───────────────────────────────────────

def test_market_snapshot_shape(hl_mock):
    snap = svc.get_client().market_snapshot()
    assert set(snap) == {"markets", "delisted", "fetched_at_ms"}
    assert isinstance(snap["fetched_at_ms"], int)
    assert "MATIC" in snap["delisted"]
    assert hl_mock.requests == [{"type": "metaAndAssetCtxs"}]


def test_resolve_and_get_market(hl_mock):
    c = svc.get_client()
    assert c.resolve_coin("KPEPE") == "kPEPE"
    assert c.get_market("kpepeusdt")["coin"] == "kPEPE"
    assert set(c.get_market("BTC")) == MARKET_KEYS


@pytest.mark.parametrize("sym,delisted", [("NOTACOIN", False), ("MATIC", True)])
def test_unknown_or_delisted_sends_no_data_request(hl_mock, sym, delisted):
    c = svc.get_client()
    for call in (
        lambda: c.candles(sym, "1h", bars=10),
        lambda: c.funding_history(sym),
        lambda: c.l2_book(sym),
        lambda: c.get_market(sym),
    ):
        with pytest.raises(ScreenerServiceError) as ei:
            call()
        e = err_of(ei)
        assert e["code"] == "SYMBOL_NOT_FOUND" and e["exchange"] == "HYPERLIQUID"
        assert bool(e.get("delisted")) is delisted
    assert not hl_mock.of_type("candleSnapshot")
    assert not hl_mock.of_type("fundingHistory")
    assert not hl_mock.of_type("l2Book")


def test_candle_request_body_uses_canonical_coin_and_interval(hl_mock):
    res = svc.get_client().candles("KPEPE", "1D", bars=10)
    (body,) = hl_mock.of_type("candleSnapshot")
    assert body["req"]["coin"] == "kPEPE"
    assert body["req"]["interval"] == "1d"
    assert body["req"]["endTime"] == NOW_FLOOR
    assert body["req"]["startTime"] == NOW_FLOOR - 10 * DAY
    assert res["coin"] == "kPEPE" and res["interval"] == "1d"
    assert res["start_ms"] == NOW_FLOOR - 10 * DAY and res["end_ms"] == NOW_FLOOR
    assert res["requested_bars"] == 10 and res["truncated"] is False and res["warning"] is None
    assert set(res) == {"coin", "interval", "start_ms", "end_ms", "requested_bars", "truncated", "warning", "candles"}
    assert res["candles"] == svc.parse_candles(fx("candle_snapshot_kPEPE_1d.json"))


def test_monthly_interval_stays_uppercase(hl_mock):
    svc.get_client().candles("BTC", "1M", bars=3)
    assert hl_mock.of_type("candleSnapshot")[0]["req"]["interval"] == "1M"


def test_default_bar_count(hl_mock):
    res = svc.get_client().candles("BTC", "1h")
    assert res["requested_bars"] == 300
    assert res["end_ms"] - res["start_ms"] == 300 * HOUR


def test_candles_empty_upstream_returns_empty(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(200, json=[])])
    res = svc.get_client().candles("BTC", "1h", bars=5)
    assert res["candles"] == []


def test_funding_request_body_and_clamp(hl_mock):
    svc.get_client().funding_history("kpepe", hours=10_000)
    (body,) = hl_mock.of_type("fundingHistory")
    assert body["coin"] == "kPEPE"
    assert "startTime" in body
    assert body["endTime"] - body["startTime"] == 500 * HOUR
    assert body["endTime"] == NOW_FLOOR
    hl_mock.requests.clear()
    svc.reset_state()
    svc.get_client().funding_history("BTC", hours=0)
    assert hl_mock.of_type("fundingHistory")[0]["endTime"] - hl_mock.of_type("fundingHistory")[0]["startTime"] == HOUR


def test_funding_history_default_window_and_parse(hl_mock):
    rows = svc.get_client().funding_history("BTC")
    (body,) = hl_mock.of_type("fundingHistory")
    assert body["endTime"] - body["startTime"] == 72 * HOUR
    assert rows == svc.parse_funding_history(fx("funding_history_BTC.json"))


def test_l2_request_body_and_clamp(hl_mock):
    book = svc.get_client().l2_book("btc-perp", depth=999)
    (body,) = hl_mock.of_type("l2Book")
    assert body == {"type": "l2Book", "coin": "BTC"}
    assert len(book["bids"]) <= 20 and len(book["asks"]) <= 20
    assert len(svc.get_client().l2_book("BTC", depth=0)["bids"]) == 1


def test_l2_null_book_is_no_data(hl_mock):
    hl_mock.script("l2Book", [httpx.Response(200, content=b"null")])
    with pytest.raises(ScreenerServiceError) as ei:
        svc.get_client().l2_book("BTC")
    assert err_of(ei)["code"] == "NO_DATA"


def test_post_uses_post_json_and_content_type(hl_mock):
    seen = {}

    def spy(request):
        seen["method"] = request.method
        seen["ct"] = request.headers.get("content-type")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=hl_mock.meta)

    hl_mock.script("metaAndAssetCtxs", [spy])
    svc.get_client().market_snapshot()
    assert seen["method"] == "POST"
    assert seen["ct"].startswith("application/json")
    assert seen["url"] == "https://api.hyperliquid.xyz/info"


# ── truncation ───────────────────────────────────────────────────────────────

def test_candles_start_ms_over_5000_bars_is_truncated(hl_mock):
    end = NOW_FLOOR
    res = svc.get_client().candles("BTC", "1h", start_ms=end - 365 * DAY, end_ms=end)
    assert res["truncated"] is True
    assert res["requested_bars"] == 8760
    assert "5,000" in res["warning"]
    assert res["warning"] == (
        "Hyperliquid serves only the most recent 5,000 1h candles; "
        "requested ~8760 bars, so older data is missing."
    )


def test_candles_bars_clamped_not_truncated(hl_mock):
    res = svc.get_client().candles("BTC", "1h", bars=6000)
    assert res["requested_bars"] == 5000
    assert res["truncated"] is False and res["warning"] is None


def test_candles_bars_and_start_are_exclusive(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.get_client().candles("BTC", "1h", bars=10, start_ms=NOW_FLOOR - DAY)
    assert err_of(ei)["code"] == "INVALID_PARAMETER"
    assert hl_mock.of_type("candleSnapshot") == []


def test_fetch_ohlcv_one_year_hourly_truncated(hl_mock):
    r = svc.fetch_hyperliquid_ohlcv("BTC", "1y", "1h")
    assert r["truncated"] is True and r["requested_bars"] == 8760 and r["warning"]
    assert set(r) == {"coin", "interval", "period", "candles", "requested_bars", "returned_bars",
                      "truncated", "warning", "source"}
    assert r["source"] == "hyperliquid" and r["coin"] == "BTC" and r["interval"] == "1h" and r["period"] == "1y"
    assert r["returned_bars"] == len(r["candles"]) > 0
    assert set(r["candles"][0]) == {"date", "open", "high", "low", "close", "volume"}


def test_fetch_ohlcv_six_months_hourly_not_truncated(hl_mock):
    r = svc.fetch_hyperliquid_ohlcv("BTC", "6mo", "1h")
    assert r["truncated"] is False and r["requested_bars"] == 4320 and r["warning"] is None


def test_fetch_ohlcv_two_years_daily_not_truncated(hl_mock):
    r = svc.fetch_hyperliquid_ohlcv("BTC", "2y", "1d")
    assert r["truncated"] is False and r["warning"] is None and r["requested_bars"] == 730
    assert r["interval"] == "1d"


def test_fetch_ohlcv_resolves_symbol_case(hl_mock):
    assert svc.fetch_hyperliquid_ohlcv("KPEPE", "1mo", "1d")["coin"] == "kPEPE"


def test_fetch_ohlcv_zero_candles_is_no_data(hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(200, json=[])])
    with pytest.raises(ScreenerServiceError) as ei:
        svc.fetch_hyperliquid_ohlcv("BTC", "1mo", "1d")
    e = err_of(ei)
    assert e["code"] == "NO_DATA" and e["retryable"] is False and e["exchange"] == "HYPERLIQUID"


def test_fetch_ohlcv_bad_period_and_interval(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.fetch_hyperliquid_ohlcv("BTC", "5y", "1d")
    assert err_of(ei)["code"] == "INVALID_PARAMETER"
    with pytest.raises(ScreenerServiceError) as ei:
        svc.fetch_hyperliquid_ohlcv("BTC", "1y", "30m")
    assert err_of(ei)["code"] == "INVALID_TIMEFRAME"


# ── sync/async parity and discipline ─────────────────────────────────────────

async def test_sync_async_parity(hl_mock):
    calls = {
        "market_snapshot": lambda c: c.market_snapshot(),
        "get_market": lambda c: c.get_market("kPEPE"),
        "resolve_coin": lambda c: c.resolve_coin("1000PEPE"),
        "candles": lambda c: c.candles("BTC", "1h", bars=50),
        "funding_history": lambda c: c.funding_history("BTC", hours=48),
        "l2_book": lambda c: c.l2_book("BTC", depth=7),
    }
    for name, call in calls.items():
        svc.reset_state()
        sync_res = call(svc.HyperliquidClient(transport=hl_mock.transport))
        svc.reset_state()
        res = call(svc.AsyncHyperliquidClient(transport=hl_mock.transport))
        async_res = await res
        assert sync_res == async_res, name


def test_method_names_and_coroutine_discipline():
    for name in PUBLIC:
        assert inspect.iscoroutinefunction(getattr(svc.AsyncHyperliquidClient, name)), name
        assert not inspect.iscoroutinefunction(getattr(svc.HyperliquidClient, name)), name


def test_default_client_types():
    assert isinstance(svc.get_client(), svc.HyperliquidClient)
    assert isinstance(svc.get_async_client(), svc.AsyncHyperliquidClient)


async def test_sync_client_works_inside_running_loop(hl_mock):
    asyncio.get_running_loop()
    snap = svc.get_client().market_snapshot()
    assert snap["markets"]


async def test_async_client_default_works(hl_mock):
    snap = await svc.get_async_client().market_snapshot()
    assert any(m["coin"] == "BTC" for m in snap["markets"])


# ── shared cache ─────────────────────────────────────────────────────────────

async def test_cache_shared_between_sync_and_async(hl_mock):
    svc.get_client().market_snapshot()
    await svc.get_async_client().market_snapshot()
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1


async def test_cache_disabled_with_ttl_zero(hl_mock, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_TTL", "0")
    svc.get_client().market_snapshot()
    await svc.get_async_client().market_snapshot()
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 2


def test_cache_keys_are_per_body(hl_mock):
    c = svc.get_client()
    c.post_info({"type": "l2Book", "coin": "BTC"})
    c.post_info({"type": "l2Book", "coin": "ETH"})
    c.post_info({"type": "l2Book", "coin": "BTC"})
    c.post_info({"coin": "ETH", "type": "l2Book"})  # key order must not matter
    assert [b["coin"] for b in hl_mock.of_type("l2Book")] == ["BTC", "ETH"]


def test_snapshot_fetched_at_is_cache_insertion_time(hl_mock, monkeypatch):
    c = svc.get_client()
    first = c.market_snapshot()
    monkeypatch.setattr(svc, "_now_ms", lambda: NOW + 30_000)
    second = c.market_snapshot()
    assert second["fetched_at_ms"] == first["fetched_at_ms"] == NOW


def test_errors_are_not_cached(hl_mock):
    body = {"type": "l2Book", "coin": "BTC"}
    hl_mock.script("l2Book", [httpx.Response(500, text="boom")])
    c = svc.get_client()
    with pytest.raises(ScreenerServiceError):
        c.post_info(body)
    n = len(hl_mock.of_type("l2Book"))
    hl_mock.scripts.clear()
    assert c.post_info(body)["coin"] == "BTC"
    assert len(hl_mock.of_type("l2Book")) == n + 1


def test_null_payload_is_not_cached(hl_mock):
    body = {"type": "l2Book", "coin": "NOPE"}
    c = svc.get_client()
    assert c.post_info(body) is None
    assert c.post_info(body) is None
    assert len(hl_mock.of_type("l2Book")) == 2


# ── retries and error mapping ────────────────────────────────────────────────

BODY = {"type": "l2Book", "coin": "BTC"}


async def _post(mode, mock, body=BODY):
    if mode == "sync":
        return svc.HyperliquidClient(transport=mock.transport).post_info(body)
    return await svc.AsyncHyperliquidClient(transport=mock.transport).post_info(body)


async def _post_err(mode, mock, body=BODY) -> dict:
    with pytest.raises(ScreenerServiceError) as ei:
        await _post(mode, mock, body)
    e = err_of(ei)
    assert e["exchange"] == "HYPERLIQUID"
    return e


modes = pytest.mark.parametrize("mode", ["sync", "async"])


@modes
async def test_retry_500_500_then_success(hl_mock, mode):
    ok = httpx.Response(200, json=fx("l2_book_BTC.json"))
    hl_mock.script("l2Book", [httpx.Response(500), httpx.Response(500), ok])
    res = await _post(mode, hl_mock)
    assert res["coin"] == "BTC"
    assert len(hl_mock.of_type("l2Book")) == 3


@modes
async def test_persistent_5xx(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.Response(503)])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_ERROR" and e["retryable"] is True
    assert e["retry_after_s"] == 30 and e["status_code"] == 503
    assert len(hl_mock.of_type("l2Book")) == 3


@modes
async def test_429_default_retry_after(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.Response(429)])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_RATE_LIMIT" and e["retryable"] is True
    assert e["retry_after_s"] == 60
    assert len(hl_mock.of_type("l2Book")) == 3


@modes
async def test_429_honours_retry_after_header(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.Response(429, headers={"Retry-After": "7"})])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_RATE_LIMIT" and e["retry_after_s"] == 7


@modes
async def test_timeouts(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.ReadTimeout("slow")])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_TIMEOUT" and e["retryable"] is True and e["retry_after_s"] == 10
    assert len(hl_mock.of_type("l2Book")) == 3


@modes
async def test_transport_errors(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.ConnectError("refused")])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_ERROR" and e["retryable"] is True and e["retry_after_s"] == 30
    assert len(hl_mock.of_type("l2Book")) == 3


@modes
async def test_422_is_not_retried(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.Response(422, text="Failed to deserialize")])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_ERROR" and e["retryable"] is False and e["status_code"] == 422
    assert len(hl_mock.of_type("l2Book")) == 1


@modes
async def test_non_json_body(hl_mock, mode):
    hl_mock.script("l2Book", [httpx.Response(200, text="<html>gateway</html>")])
    e = await _post_err(mode, hl_mock)
    assert e["code"] == "UPSTREAM_ERROR" and e["retryable"] is True
    assert len(hl_mock.of_type("l2Book")) == 3


async def test_public_methods_wrap_httpx_errors(hl_mock):
    hl_mock.script("metaAndAssetCtxs", [httpx.ConnectError("refused")])
    with pytest.raises(ScreenerServiceError):
        svc.get_client().market_snapshot()
    with pytest.raises(ScreenerServiceError):
        await svc.get_async_client().market_snapshot()


# ── async concurrency ────────────────────────────────────────────────────────

async def test_async_concurrency_limit_does_not_block_loop(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_MCP_MAX_INFLIGHT", "2")
    svc.reset_state()
    state = {"cur": 0, "peak": 0}

    async def handler(request):
        state["cur"] += 1
        state["peak"] = max(state["peak"], state["cur"])
        try:
            await asyncio.sleep(0.05)
        finally:
            state["cur"] -= 1
        return httpx.Response(200, json=fx("l2_book_BTC.json"))

    client = svc.AsyncHyperliquidClient(transport=httpx.MockTransport(handler))
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0.005)
            ticks += 1

    t = asyncio.create_task(ticker())
    bodies = [{"type": "l2Book", "coin": f"C{i}"} for i in range(6)]
    results = await asyncio.gather(*(client.post_info(b) for b in bodies))
    done = True
    await t
    assert len(results) == 6
    assert 1 <= state["peak"] <= 2
    assert ticks >= 3


# ── shared weight budget ─────────────────────────────────────────────────────

@pytest.mark.parametrize("third", ["sync", "async"])
async def test_weight_budget_shared_between_sync_and_async(hl_mock, monkeypatch, third):
    monkeypatch.setenv("HYPERLIQUID_MCP_WEIGHT_PER_MIN", "40")
    svc.reset_state()
    clock = [1000.0]
    waits: list[float] = []

    def sleep(s):
        waits.append(s)
        clock[0] += s

    async def asleep(s):
        waits.append(s)
        clock[0] += s

    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(svc, "_sleep", sleep)
    monkeypatch.setattr(svc, "_async_sleep", asleep)
    # Empty list responses add no extra response weight, so each request costs exactly 20.
    hl_mock.script("fundingHistory", [httpx.Response(200, json=[])])

    def body(i):
        return {"type": "fundingHistory", "coin": "BTC", "startTime": i, "endTime": i + 1}

    svc.get_client().post_info(body(1))
    await svc.get_async_client().post_info(body(2))
    assert not [w for w in waits if w > 0], "first two requests fit the 40-weight bucket"
    if third == "sync":
        svc.get_client().post_info(body(3))
    else:
        await svc.get_async_client().post_info(body(3))
    assert any(w > 0 for w in waits), "third request must wait for refill"
    assert len(hl_mock.of_type("fundingHistory")) == 3
