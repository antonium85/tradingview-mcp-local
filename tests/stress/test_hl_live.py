"""Live Hyperliquid checks (real network). Run only on request:

    uv run pytest -m stress tests/stress/test_hl_live.py

Every test is bounded by HL_LIVE_CEILING_S via asyncio.wait_for (same idiom as
test_real_upstream.py). Sync calls run in a worker thread; wait_for fails the
test at the ceiling but cannot kill the thread, which stops within the httpx
bound set by the autouse fixture (about 26 s per request).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tradingview_mcp import hyperliquid_tools as ht
from tradingview_mcp import server
from tradingview_mcp.core.services import hyperliquid_service as svc
from tradingview_mcp.core.utils.validators import COINLIST_DIR

pytestmark = pytest.mark.stress

HL_LIVE_CEILING_S = 30.0


@pytest.fixture(autouse=True)
def _real_clients(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_MCP_TIMEOUT_S", "8")
    monkeypatch.setenv("HYPERLIQUID_MCP_RETRY_DELAYS", "0.5,1.0")
    svc.configure_default_clients()  # real transports
    yield
    monkeypatch.undo()
    svc.reset_state()


async def bounded(coro):
    return await asyncio.wait_for(coro, timeout=HL_LIVE_CEILING_S)


async def in_thread(fn, *a, **k):
    return await bounded(asyncio.to_thread(fn, *a, **k))


async def test_market_overview_contains_btc():
    res = await bounded(ht.hyperliquid_market_overview(limit=100))
    assert "error" not in res, res
    assert any(m["coin"] == "BTC" for m in res["markets"])


async def test_resolve_kpepe():
    assert await in_thread(svc.get_client().resolve_coin, "KPEPE") == "kPEPE"


async def test_candles_btc_24_bars():
    res = await in_thread(svc.get_client().candles, "BTC", "1h", bars=24)
    assert len(res["candles"]) in (24, 25)


async def test_funding_btc_history():
    res = await bounded(ht.hyperliquid_funding_rates("BTC"))
    assert "error" not in res, res
    assert res["history"]


async def test_orderbook_btc_spread():
    res = await bounded(ht.hyperliquid_orderbook("BTC"))
    assert "error" not in res, res
    assert res["spread"] > 0


def _listed() -> set[str]:
    with open(os.path.join(COINLIST_DIR, "hyperliquid.txt"), encoding="utf-8") as f:
        return {ln.strip().split(":", 1)[1] for ln in f if ln.strip()}


async def test_coinlist_drift():
    snap = await in_thread(svc.get_client().market_snapshot)
    live = {m["coin"] for m in snap["markets"]}
    listed = _listed()
    covered = len(live & listed) / len(live)
    assert covered >= 0.90, (
        f"coinlist covers only {covered:.0%} of live markets; "
        "regenerate with: uv run python scripts/gen_hyperliquid_coinlist.py"
    )


async def test_coinlist_reverse_drift():
    snap = await in_thread(svc.get_client().market_snapshot)
    live = {m["coin"] for m in snap["markets"]}
    listed = _listed()
    frac = len(listed & live) / len(listed)
    assert frac >= 0.90, (
        f"only {frac:.0%} of hyperliquid.txt is still live; stale names: "
        f"{sorted(listed - live)[:20]}; regenerate with: "
        "uv run python scripts/gen_hyperliquid_coinlist.py"
    )


async def test_top_gainers_1d():
    res = await bounded(server.top_gainers(exchange="HYPERLIQUID", timeframe="1D", limit=5))
    assert isinstance(res, list) and len(res) == 5, res
    assert all(r["source"] == "hyperliquid" for r in res)


async def test_coin_analysis_btc_1h():
    res = await in_thread(server.coin_analysis, "BTC", "HYPERLIQUID", "1h")
    assert "error" not in res, res
    assert res["bars_used"] >= 250


async def test_multi_timeframe_btc():
    res = await bounded(server.multi_timeframe_analysis("BTC", "HYPERLIQUID"))
    assert "error" not in res, res
    ok = [tf for tf, e in res["timeframes"].items() if "error" not in e]
    assert len(ok) >= 4, res["timeframes"]


async def test_backtest_1d_not_truncated():
    res = await in_thread(server.backtest_strategy, "HYPERLIQUID:BTC", "rsi", "1y", interval="1d")
    assert "error" not in res, res
    assert res["data_source"] == "Hyperliquid"
    assert res["truncated"] is False


async def test_backtest_1h_truncated():
    res = await in_thread(server.backtest_strategy, "HYPERLIQUID:BTC", "rsi", "1y", interval="1h")
    assert "error" not in res, res
    assert res["truncated"] is True
    assert 4900 <= res["returned_bars"] <= 5010


async def test_smart_money_eth():
    res = await in_thread(server.smart_money_analysis, "HYPERLIQUID:ETH")
    assert "error" not in res, res
