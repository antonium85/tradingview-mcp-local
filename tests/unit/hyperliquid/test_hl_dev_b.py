"""WP-B developer tests: hyperliquid_service internals.

Covers the concurrency discipline (sync/async primitives never cross, no lock
held across sleep, per-loop semaphores), backoff/jitter, TTL expiry, the
weight bucket, R6 rounding and a few edge paths. The conftest's autouse
fixture points the default clients at the fixture-backed ``hl_mock``.
"""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from tradingview_mcp.core.errors import ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_service as svc

BOOK = {"type": "l2Book", "coin": "BTC"}


def _err(ei) -> dict:
    return ei.value.to_envelope()["error"]


def _fresh(status: int, **kw):
    """Script item building a NEW Response per request (httpx binds streams to one client)."""
    return lambda request: httpx.Response(status, **kw)


class _Tripwire:
    """Stand-in for a primitive the current path must never touch."""

    def __getattr__(self, name):
        raise AssertionError(f"forbidden primitive touched: {name}")

    def __enter__(self):
        raise AssertionError("forbidden primitive entered")

    def __exit__(self, *a):
        return False


# ── sync/async primitives never cross ───────────────────────────────────────

async def test_async_path_never_touches_threading_semaphore(monkeypatch):
    monkeypatch.setattr(svc, "_SYNC_SEMAPHORE", _Tripwire())
    snap = await svc.get_async_client().market_snapshot()
    assert snap["markets"]


def test_sync_path_never_touches_asyncio_semaphores(monkeypatch):
    def boom():
        raise AssertionError("sync path asked for an asyncio semaphore")

    monkeypatch.setattr(svc, "_async_semaphore", boom)
    monkeypatch.setattr(svc, "_throttle_async", boom)
    assert svc.get_client().market_snapshot()["markets"]


def test_async_semaphore_is_per_loop(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_MCP_MAX_INFLIGHT", "3")
    svc.reset_state()
    got: list = []

    async def grab():
        a, b = svc._async_semaphore(), svc._async_semaphore()
        assert a is b
        got.append(a)

    def run_in_new_loop():
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(grab())
        finally:
            loop.close()

    for _ in range(2):
        t = threading.Thread(target=run_in_new_loop)
        t.start()
        t.join()
    assert got[0] is not got[1]
    assert all(isinstance(s, asyncio.Semaphore) for s in got)
    assert got[0]._value == 3


def test_sync_client_from_worker_threads_respects_max_inflight(monkeypatch, hl_mock):
    monkeypatch.setenv("HYPERLIQUID_MCP_MAX_INFLIGHT", "2")
    svc.reset_state()

    def slow(request):
        time.sleep(0.05)
        return httpx.Response(200, json={"coin": "BTC", "time": 1, "levels": [[], []]})

    hl_mock.script("l2Book", [slow])
    client = svc.get_client()
    threads = [threading.Thread(target=client.post_info, args=({"type": "l2Book", "coin": f"C{i}"},))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert hl_mock.count == 6
    assert hl_mock.peak <= 2


async def test_sync_client_works_inside_running_loop():
    # No asyncio.run, no awaiting: the sync client must not care about the loop.
    assert svc.get_client().resolve_coin("kpepe") == "kPEPE"


# ── no lock held while sleeping ─────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_throttle_sleep_holds_no_lock(monkeypatch, mode):
    monkeypatch.setenv("HYPERLIQUID_MCP_WEIGHT_PER_MIN", "20")
    svc.reset_state()
    clock = [100.0]
    observed: list[tuple] = []

    def check(s):
        observed.append((s, svc._BUDGET._lock.locked(), svc._CACHE_LOCK.locked(),
                         svc._ASYNC_SEM_LOCK.locked()))
        clock[0] += s

    async def acheck(s):
        check(s)

    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(svc, "_sleep", check)
    monkeypatch.setattr(svc, "_async_sleep", acheck)
    for i in range(2):
        body = {"type": "metaAndAssetCtxs", "n": i}  # distinct keys, weight 20 each
        if mode == "sync":
            svc.get_client().post_info(body)
        else:
            await svc.get_async_client().post_info(body)
    assert observed, "second request should have waited for refill"
    assert all(s > 0 and not any(locks) for s, *locks in observed)
    assert observed[0][0] == pytest.approx(60.0)   # 20 tokens at 20/min


# ── backoff ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_zero_delays_never_sleep(monkeypatch, hl_mock, mode):
    slept: list[float] = []

    async def asleep(s):
        slept.append(s)

    monkeypatch.setattr(svc, "_sleep", slept.append)
    monkeypatch.setattr(svc, "_async_sleep", asleep)
    hl_mock.script("l2Book", [_fresh(503, text="busy")])
    with pytest.raises(ScreenerServiceError):
        if mode == "sync":
            svc.get_client().post_info(BOOK)
        else:
            await svc.get_async_client().post_info(BOOK)
    assert len(hl_mock.of_type("l2Book")) == 3
    assert slept == []


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_backoff_uses_jittered_env_delays(monkeypatch, hl_mock, mode):
    monkeypatch.setenv("HYPERLIQUID_MCP_RETRY_DELAYS", "1.0,2.0")
    slept: list[float] = []

    async def asleep(s):
        slept.append(s)

    monkeypatch.setattr(svc, "_sleep", slept.append)
    monkeypatch.setattr(svc, "_async_sleep", asleep)
    hl_mock.script("l2Book", [_fresh(502, text="bad gateway")])
    with pytest.raises(ScreenerServiceError) as ei:
        if mode == "sync":
            svc.get_client().post_info(BOOK)
        else:
            await svc.get_async_client().post_info(BOOK)
    assert _err(ei)["status_code"] == 502
    assert len(slept) == 2   # no sleep after the final attempt
    assert 0.8 <= slept[0] <= 1.2 and 1.6 <= slept[1] <= 2.4


def test_retry_after_non_numeric_defaults_to_60(hl_mock):
    hl_mock.script("l2Book", [_fresh(429, headers={"Retry-After": "soon"})])
    with pytest.raises(ScreenerServiceError) as ei:
        svc.get_client().post_info(BOOK)
    e = _err(ei)
    assert e["code"] == "UPSTREAM_RATE_LIMIT" and e["retry_after_s"] == 60


def test_success_after_429_is_cached(hl_mock):
    book = {"coin": "BTC", "time": 5, "levels": [[], []]}
    hl_mock.script("l2Book", [_fresh(429), _fresh(200, json=book)])
    c = svc.get_client()
    assert c.post_info(BOOK) == book
    assert c.post_info(BOOK) == book
    assert len(hl_mock.of_type("l2Book")) == 2


# ── cache ───────────────────────────────────────────────────────────────────

def test_cache_expires_after_ttl_and_keeps_fetch_time(monkeypatch, hl_mock):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_TTL", "60")
    clock = [0.0]
    wall = [1_000_000]
    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(svc, "_now_ms", lambda: wall[0])
    c = svc.get_client()

    assert c.market_snapshot()["fetched_at_ms"] == 1_000_000
    clock[0], wall[0] = 59.0, 1_059_000
    assert c.market_snapshot()["fetched_at_ms"] == 1_000_000   # cache insertion time
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1
    clock[0], wall[0] = 61.0, 1_061_000
    assert c.market_snapshot()["fetched_at_ms"] == 1_061_000
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 2


def test_malformed_snapshot_is_typed_upstream_error(hl_mock):
    hl_mock.script("metaAndAssetCtxs", [_fresh(200, json={"nope": 1})])
    with pytest.raises(ScreenerServiceError) as ei:
        svc.get_client().resolve_coin("BTC")
    e = _err(ei)
    assert e["code"] == "UPSTREAM_ERROR" and e["retryable"] is True
    assert e["exchange"] == "HYPERLIQUID"


# ── weight bucket ───────────────────────────────────────────────────────────

def test_weight_budget_reserve_charge_refill(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])
    b = svc._WeightBudget(60)            # 1 token per second
    assert b.reserve(40) == 0.0
    assert b.reserve(40) == pytest.approx(20.0)   # 20 left, needs 20 more seconds
    b.charge(30)                          # may go negative
    assert b.reserve(1) == pytest.approx(11.0)
    clock[0] = 1000.0
    assert b.reserve(60) == 0.0           # refill capped at capacity
    assert b.reserve(1) > 0


def test_weight_above_capacity_cannot_deadlock(monkeypatch):
    monkeypatch.setattr(svc, "_monotonic", lambda: 0.0)
    b = svc._WeightBudget(10)
    assert b.reserve(20) == 0.0           # clamped to capacity


def test_response_extra_weight_is_charged(monkeypatch, hl_mock):
    clock = [0.0]
    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])
    svc.reset_state()
    hl_mock.script("candleSnapshot", [_fresh(200, json=[{"t": i, "T": i, "o": "1", "h": "1", "l": "1",
                                                          "c": "1", "v": "1", "n": 1}
                                                         for i in range(120)])])
    svc.get_client().post_info({"type": "candleSnapshot", "req": {"coin": "BTC"}})
    assert svc._BUDGET._tokens == pytest.approx(1000 - 20 - 2)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_r6_rounding_and_date_formats():
    candles = [{"t": 1790391600000, "open": 85361.123456, "high": 1.000049, "low": 0.004283,
                "close": 0.00017412345, "volume": 541.540319999}]
    daily = svc.to_backtest_candles(candles, "1d")[0]
    assert daily["date"] == "2026-09-26"
    assert daily["open"] == 85361.1235
    assert daily["high"] == 1.0           # >= $1: 4 dp
    assert daily["low"] == 0.004283       # < $1: 6 significant digits
    assert daily["close"] == 0.000174123
    assert daily["volume"] == 541.540319999
    assert svc.to_backtest_candles(candles, "1h")[0]["date"] == "2026-09-26 03:00"
    for iv in ("1w", "1M"):
        assert svc.to_backtest_candles(candles, iv)[0]["date"] == "2026-09-26"
    assert svc.to_backtest_candles(candles, "15m")[0]["date"] == "2026-09-26 03:00"


def test_funding_time_is_second_precision_utc():
    rows = svc.parse_funding_history([
        {"coin": "BTC", "fundingRate": "-0.0000125", "premium": None, "time": 1790856000034},
        {"coin": "BTC", "fundingRate": "0.0000125", "premium": "-0.0003", "time": 1790852400010},
    ])
    assert [r["time"] for r in rows] == ["2026-10-01T11:00:00Z", "2026-10-01T12:00:00Z"]
    assert rows[1]["premium"] is None and rows[1]["funding_rate_1h"] < 0


@pytest.mark.parametrize("bad", [None, "", "   ", "2h"])
def test_map_timeframe_errors_carry_exchange(bad):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.map_timeframe(bad)
    e = _err(ei)
    assert e["code"] == "INVALID_TIMEFRAME" and e["exchange"] == "HYPERLIQUID"
    assert e["valid_timeframes"] == sorted(svc.TIMEFRAME_MAP)


def test_rank_markets_ties_keep_input_order():
    rows = [{"c": "a", "v": 1}, {"c": "b", "v": 2}, {"c": "x", "v": None}, {"c": "d", "v": 2}]
    assert [r["c"] for r in svc.rank_markets(rows, "v")] == ["b", "d", "a"]
    assert [r["c"] for r in svc.rank_markets(rows, "v", descending=False, limit=2)] == ["a", "b"]


# ── client edge paths ───────────────────────────────────────────────────────

def test_start_not_before_end_is_invalid_without_network(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.get_client().candles("BTC", "1h", start_ms=2_000, end_ms=1_000)
    assert _err(ei)["code"] == "INVALID_PARAMETER"
    assert hl_mock.count == 0


def test_body_without_type_is_rejected(hl_mock):
    with pytest.raises(ScreenerServiceError) as ei:
        svc.get_client().post_info({"coin": "BTC"})
    assert _err(ei)["code"] == "INVALID_PARAMETER"
    assert hl_mock.count == 0


async def test_get_market_sync_async_equal():
    s = svc.get_client().get_market("1000PEPEUSDT")
    a = await svc.get_async_client().get_market("kpepe")
    assert s == a and s["coin"] == "kPEPE"


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_null_book_is_no_data_with_coin(hl_mock, mode):
    hl_mock.script("l2Book", [_fresh(200, content=b"null")])
    with pytest.raises(ScreenerServiceError) as ei:
        if mode == "sync":
            svc.get_client().l2_book("btc", depth=99)
        else:
            await svc.get_async_client().l2_book("btc", depth=99)
    e = _err(ei)
    assert e["code"] == "NO_DATA" and e["retryable"] is False and e["symbol"] == "BTC"


async def test_book_reports_resolved_coin_not_payload_coin():
    # The fixture mock serves the BTC book for any known coin.
    s = svc.get_client().l2_book("eth", depth=3)
    a = await svc.get_async_client().l2_book("ETH-PERP", depth=3)
    assert s == a and s["coin"] == "ETH" and len(s["bids"]) == 3


def test_fetch_ohlcv_zero_candles_is_no_data(hl_mock):
    hl_mock.script("candleSnapshot", [_fresh(200, json=[])])
    with pytest.raises(ScreenerServiceError) as ei:
        svc.fetch_hyperliquid_ohlcv("BTC", "1mo", "1d")
    e = _err(ei)
    assert e["code"] == "NO_DATA" and e["retryable"] is False and e["exchange"] == "HYPERLIQUID"


def test_fetch_ohlcv_shape(hl_mock):
    res = svc.fetch_hyperliquid_ohlcv("kpepe", " 3MO ", "1D")
    assert res["coin"] == "kPEPE" and res["interval"] == "1d" and res["period"] == "3mo"
    assert res["requested_bars"] == 90 and res["truncated"] is False and res["warning"] is None
    assert res["returned_bars"] == len(res["candles"]) > 0
    assert res["source"] == "hyperliquid"
    req = hl_mock.of_type("candleSnapshot")[-1]["req"]
    assert req["coin"] == "kPEPE" and req["interval"] == "1d"
    assert req["endTime"] - req["startTime"] == 90 * 86_400_000


def test_configure_default_clients_separate_async_transport(hl_mock):
    seen: list[str] = []

    async def async_handler(request):
        seen.append("async")
        return httpx.Response(200, json=hl_mock.meta)

    svc.configure_default_clients(transport=hl_mock.transport,
                                  async_transport=httpx.MockTransport(async_handler))
    svc.get_client().post_info({"type": "metaAndAssetCtxs", "k": 1})
    asyncio_result = asyncio.new_event_loop()
    try:
        asyncio_result.run_until_complete(
            svc.get_async_client().post_info({"type": "metaAndAssetCtxs", "k": 2}))
    finally:
        asyncio_result.close()
    assert seen == ["async"]
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1


# ── review fixes: no cache poisoning, cache cap, client close ───────────────

def _meta_one_ctx_short(meta):
    m, ctxs = meta
    return [m, ctxs[:-1]]


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_unparseable_snapshot_is_not_served_from_cache(hl_mock, mode):
    hl_mock.script("metaAndAssetCtxs", [_fresh(200, json=_meta_one_ctx_short(hl_mock.meta)),
                                        _fresh(200, json=hl_mock.meta)])
    client = svc.get_client() if mode == "sync" else svc.get_async_client()

    async def snap():
        r = client.market_snapshot()
        return await r if mode == "async" else r

    with pytest.raises(ScreenerServiceError) as ei:
        await snap()
    assert _err(ei)["code"] == "UPSTREAM_ERROR" and _err(ei)["retryable"] is True
    assert (await snap())["markets"]
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 2
    await snap()                                   # the good payload IS cached
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 2


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_unparseable_candles_are_not_served_from_cache(hl_mock, mode):
    good = _fresh(200, json=[{"t": 1, "T": 2, "o": "1", "h": "1", "l": "1", "c": "1",
                              "v": "1", "n": 1}])
    hl_mock.script("candleSnapshot", [_fresh(200, json=[{"t": "x"}]), good])
    client = svc.get_client() if mode == "sync" else svc.get_async_client()

    async def candles():
        r = client.candles("BTC", "1h", bars=5, end_ms=3_600_000_000)
        return await r if mode == "async" else r

    with pytest.raises(ScreenerServiceError):
        await candles()
    assert len((await candles())["candles"]) == 1
    assert len(hl_mock.of_type("candleSnapshot")) == 2


def test_cache_is_capped_oldest_first(monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_MCP_CACHE_TTL", "3600")
    clock = [0.0]
    monkeypatch.setattr(svc, "_monotonic", lambda: clock[0])
    for i in range(300):
        clock[0] = float(i)
        svc._cache_put(f"k{i}", [i])
    assert len(svc._CACHE) == svc._CACHE_MAX_ENTRIES == 256
    assert "k0" not in svc._CACHE and "k43" not in svc._CACHE
    assert "k44" in svc._CACHE and "k299" in svc._CACHE


def test_configure_default_clients_closes_old_sync_client(hl_mock):
    old = svc.get_client()
    svc.configure_default_clients(transport=hl_mock.transport)
    assert old._client.is_closed
    assert not svc.get_client()._client.is_closed
