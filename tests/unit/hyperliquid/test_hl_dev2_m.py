"""WP-M: Hyperliquid top movers."""
from __future__ import annotations

import asyncio

import pytest

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_movers as mv
from tradingview_mcp.core.services import hyperliquid_service as hl_svc

ROW_KEYS = {"symbol", "changePercent", "indicators", "volume_24h_usd", "open_interest_usd",
            "funding_rate_1h", "change_window", "exchange", "source"}


def _expected(hl_mock, losers, limit):
    markets, _ = hl_svc.parse_markets(hl_mock.meta)
    rows = [m for m in markets if m["change_pct_24h"] is not None]
    rows = sorted(rows, key=lambda m: m["change_pct_24h"], reverse=not losers)
    return rows[:limit]


@pytest.mark.parametrize("losers", [False, True])
def test_sync_rankings(hl_mock, no_network, losers):
    rows = mv.hyperliquid_top_movers("1D", losers=losers, limit=5)
    exp = _expected(hl_mock, losers, 5)
    assert [r["symbol"] for r in rows] == [f"HYPERLIQUID:{m['coin']}" for m in exp]
    assert all(set(r) == ROW_KEYS for r in rows)
    assert rows[0]["indicators"] == {"open": exp[0]["prev_day_price"], "close": exp[0]["mark_price"],
                                     "volume": exp[0]["volume_24h_base"]}
    assert rows[0]["change_window"] == "24h" and rows[0]["source"] == "hyperliquid"
    assert len(hl_mock.of_type("metaAndAssetCtxs")) == 1 and hl_mock.count == 1


def test_async_matches_sync_and_uses_only_async_client(hl_mock, no_network, monkeypatch):
    sync_rows = mv.hyperliquid_top_movers("1d", losers=False, limit=7)
    hl_svc.reset_state()
    hl_svc.configure_default_clients(transport=hl_mock.transport)

    def boom():
        raise AssertionError("sync client used")
    monkeypatch.setattr(hl_svc, "get_client", boom)
    rows = asyncio.run(mv.hyperliquid_top_movers_async(" 1D ", losers=False, limit=7))
    assert rows == sync_rows


def test_sync_does_not_touch_async_client(no_network, monkeypatch):
    def boom():
        raise AssertionError("async client used")
    monkeypatch.setattr(hl_svc, "get_async_client", boom)
    assert mv.hyperliquid_top_movers("1D", losers=True, limit=3)


@pytest.mark.parametrize("tf", ["15m", "1h", "", None, "1W"])
def test_bad_timeframe_zero_requests(hl_mock, no_network, tf):
    with pytest.raises(ScreenerServiceError) as ei:
        mv.hyperliquid_top_movers(tf, losers=False, limit=5)
    assert ei.value.code == ErrorCode.INVALID_TIMEFRAME
    assert ei.value.extra["valid_timeframes"] == ["1D"]
    with pytest.raises(ScreenerServiceError):
        asyncio.run(mv.hyperliquid_top_movers_async(tf, losers=True, limit=5))
    assert hl_mock.count == 0


def test_limit_clamped_and_none_dropped():
    markets = [
        {"coin": "A", "change_pct_24h": None, "prev_day_price": 1, "mark_price": 1,
         "volume_24h_base": 1, "volume_24h_usd": 1, "open_interest_usd": 1, "funding_rate_1h": 0},
    ] + [
        {"coin": f"C{i}", "change_pct_24h": float(i), "prev_day_price": 1, "mark_price": 1,
         "volume_24h_base": 1, "volume_24h_usd": 1, "open_interest_usd": 1, "funding_rate_1h": 0}
        for i in range(80)
    ]
    assert len(mv.build_mover_rows(markets, losers=False, limit=999)) == mv.MOVERS_MAX_LIMIT
    assert len(mv.build_mover_rows(markets, losers=False, limit=0)) == 1
    assert mv.build_mover_rows(markets, losers=True, limit=1)[0]["symbol"] == "HYPERLIQUID:C0"
