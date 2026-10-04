"""Live Hyperliquid checks (real network). Run only on request:

    uv run pytest -m stress tests/stress/test_hl_live.py
"""
from __future__ import annotations

import os

import pytest

from tradingview_mcp import hyperliquid_tools as ht
from tradingview_mcp.core.services import hyperliquid_service as svc
from tradingview_mcp.core.utils.validators import COINLIST_DIR

pytestmark = pytest.mark.stress


@pytest.fixture(autouse=True)
def _real_clients():
    svc.configure_default_clients()  # real transports
    yield
    svc.reset_state()


async def test_market_overview_contains_btc():
    res = await ht.hyperliquid_market_overview(limit=100)
    assert "error" not in res, res
    assert any(m["coin"] == "BTC" for m in res["markets"])


def test_resolve_kpepe():
    assert svc.get_client().resolve_coin("KPEPE") == "kPEPE"


def test_candles_btc_24_bars():
    res = svc.get_client().candles("BTC", "1h", bars=24)
    assert len(res["candles"]) in (24, 25)


async def test_funding_btc_history():
    res = await ht.hyperliquid_funding_rates("BTC")
    assert "error" not in res, res
    assert res["history"]


async def test_orderbook_btc_spread():
    res = await ht.hyperliquid_orderbook("BTC")
    assert "error" not in res, res
    assert res["spread"] > 0


def test_coinlist_drift():
    snap = svc.get_client().market_snapshot()
    live = {m["coin"] for m in snap["markets"]}
    with open(os.path.join(COINLIST_DIR, "hyperliquid.txt"), encoding="utf-8") as f:
        listed = {ln.strip().split(":", 1)[1] for ln in f if ln.strip()}
    covered = len(live & listed) / len(live)
    assert covered >= 0.90, (
        f"coinlist covers only {covered:.0%} of live markets; "
        "regenerate with: uv run python scripts/gen_hyperliquid_coinlist.py"
    )
