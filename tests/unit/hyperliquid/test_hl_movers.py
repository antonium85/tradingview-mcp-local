"""Hyperliquid top_gainers / top_losers (spec step-2 sections 2.2 and 5.3)."""
from __future__ import annotations

import importlib
import json
import pathlib

import httpx
import pytest

from tradingview_mcp import server
from tradingview_mcp.core.errors import ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_service as svc

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"
ROW_KEYS = {"symbol", "changePercent", "indicators", "volume_24h_usd", "open_interest_usd",
            "funding_rate_1h", "change_window", "exchange", "source"}


def mv():
    return importlib.import_module("tradingview_mcp.core.services.hyperliquid_movers")


def fx(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def markets():
    m, _ = svc.parse_markets(fx("meta_and_asset_ctxs.json"))
    return m


@pytest.fixture
def delisted():
    _, d = svc.parse_markets(fx("meta_and_asset_ctxs.json"))
    return d


def expected(markets, *, losers, limit):
    rows = [m for m in markets if m["change_pct_24h"] is not None]
    rows = sorted(rows, key=lambda m: m["change_pct_24h"], reverse=not losers)
    return rows[:limit]


# ── check_movers_timeframe ───────────────────────────────────────────────────

@pytest.mark.parametrize("tf", ["1D", "1d", " 1d "])
def test_check_accepts_1d(tf):
    assert mv().check_movers_timeframe(tf) is None


@pytest.mark.parametrize("tf", ["15m", "4h", "1W", "1M", ""])
def test_check_rejects_others(tf):
    with pytest.raises(ScreenerServiceError) as ei:
        mv().check_movers_timeframe(tf)
    err = ei.value.to_envelope()["error"]
    assert err["code"] == "INVALID_TIMEFRAME"
    assert err["valid_timeframes"] == ["1D"]
    assert err["exchange"] == "HYPERLIQUID"
    assert err["retryable"] is False
    assert "1D" in err["message"] and "24h" in err["message"]
    assert repr(tf) in err["message"]


def test_constants():
    assert mv().MOVERS_TIMEFRAME == "1D"
    assert mv().MOVERS_MAX_LIMIT == 50


# ── build_mover_rows ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("losers", [False, True])
def test_build_rows_sorted_and_exact(markets, losers):
    rows = mv().build_mover_rows(markets, losers=losers, limit=5)
    want = expected(markets, losers=losers, limit=5)
    assert [r["symbol"] for r in rows] == [f"HYPERLIQUID:{m['coin']}" for m in want]
    ch = [r["changePercent"] for r in rows]
    assert ch == sorted(ch, reverse=not losers)
    for r, m in zip(rows, want):
        assert set(r) == ROW_KEYS
        assert r["changePercent"] == m["change_pct_24h"]
        assert r["indicators"] == {"open": m["prev_day_price"], "close": m["mark_price"],
                                   "volume": m["volume_24h_base"]}
        assert r["indicators"]["open"] == m["prev_day_price"]
        assert r["indicators"]["close"] == m["mark_price"]
        assert r["volume_24h_usd"] == m["volume_24h_usd"]
        assert r["open_interest_usd"] == m["open_interest_usd"]
        assert r["funding_rate_1h"] == m["funding_rate_1h"]
        assert r["change_window"] == "24h"
        assert r["exchange"] == "HYPERLIQUID"
        assert r["source"] == "hyperliquid"


def test_symbol_keeps_canonical_case(markets):
    rows = mv().build_mover_rows(markets, losers=False, limit=1000)
    syms = {r["symbol"] for r in rows}
    assert "HYPERLIQUID:kPEPE" in syms
    assert "HYPERLIQUID:KPEPE" not in syms


def test_none_change_excluded(markets):
    broken = [dict(m) for m in markets]
    broken[0]["change_pct_24h"] = None
    name = f"HYPERLIQUID:{broken[0]['coin']}"
    rows = mv().build_mover_rows(broken, losers=False, limit=50)
    assert name not in {r["symbol"] for r in rows}


def test_limit_clamping(markets):
    assert len(mv().build_mover_rows(markets, losers=False, limit=0)) == 1
    live = len([m for m in markets if m["change_pct_24h"] is not None])
    assert len(mv().build_mover_rows(markets, losers=False, limit=1000)) == min(50, live)


def test_no_delisted_in_rows(markets, delisted):
    assert {"MATIC", "RNDR"} <= set(delisted)
    rows = mv().build_mover_rows(markets, losers=False, limit=50)
    names = {r["symbol"].split(":", 1)[1] for r in rows}
    assert not names & {"MATIC", "RNDR"}


# ── fetch functions ──────────────────────────────────────────────────────────

def test_sync_fetch_one_request_sync_client_only(markets, hl_mock, monkeypatch, no_network):
    def boom(*a, **k):
        raise AssertionError("async client used from a sync path")
    monkeypatch.setattr(svc, "get_async_client", boom)
    rows = mv().hyperliquid_top_movers("1D", losers=True, limit=3)
    assert [r["symbol"] for r in rows] == [
        f"HYPERLIQUID:{m['coin']}" for m in expected(markets, losers=True, limit=3)]
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1 and hl_mock.count == 1
    assert no_network == []


async def test_async_fetch_one_request_async_client_only(markets, hl_mock, monkeypatch, no_network):
    def boom(*a, **k):
        raise AssertionError("sync client used from an async path")
    monkeypatch.setattr(svc, "get_client", boom)
    rows = await mv().hyperliquid_top_movers_async("1D", losers=False, limit=3)
    assert [r["symbol"] for r in rows] == [
        f"HYPERLIQUID:{m['coin']}" for m in expected(markets, losers=False, limit=3)]
    assert hl_mock.count == 1
    assert no_network == []


def test_bad_timeframe_makes_zero_requests(hl_mock):
    with pytest.raises(ScreenerServiceError):
        mv().hyperliquid_top_movers("4h", losers=False, limit=3)
    assert hl_mock.count == 0


async def test_bad_timeframe_async_zero_requests(hl_mock):
    with pytest.raises(ScreenerServiceError):
        await mv().hyperliquid_top_movers_async("4h", losers=False, limit=3)
    assert hl_mock.count == 0


# ── tool level ───────────────────────────────────────────────────────────────

async def test_top_gainers_tool(markets, hl_mock, no_network):
    res = await server.top_gainers(exchange="HYPERLIQUID", timeframe="1D", limit=3)
    want = expected(markets, losers=False, limit=3)
    assert [r["symbol"] for r in res] == [f"HYPERLIQUID:{m['coin']}" for m in want]
    assert hl_mock.count == 1 and hl_mock.of_type("metaAndAssetCtxs")
    assert no_network == []


async def test_top_losers_tool(markets, hl_mock, no_network):
    res = server.top_losers(exchange="HYPERLIQUID", timeframe="1D", limit=3)
    if hasattr(res, "__await__"):
        res = await res
    want = expected(markets, losers=True, limit=3)
    assert [r["symbol"] for r in res] == [f"HYPERLIQUID:{m['coin']}" for m in want]
    assert hl_mock.count == 1
    assert no_network == []


@pytest.mark.parametrize("name", ["top_gainers", "top_losers"])
async def test_persistent_500_is_retryable_upstream_error(name, hl_mock, no_network):
    hl_mock.script("metaAndAssetCtxs", [httpx.Response(500, text="boom")])
    res = getattr(server, name)(exchange="HYPERLIQUID", timeframe="1D")
    if hasattr(res, "__await__"):
        res = await res
    assert isinstance(res, dict)
    assert res["error"]["code"] == "UPSTREAM_ERROR"
    assert res["error"]["retryable"] is True
