"""The three Hyperliquid MCP tools (spec step-1 section 5.5)."""
from __future__ import annotations

import datetime as dt
import inspect
import json
import pathlib
import statistics

import httpx
import pytest

from tradingview_mcp import server
from tradingview_mcp import hyperliquid_tools as ht
from tradingview_mcp.core.services import hyperliquid_service as svc
from tradingview_mcp.core.utils.validators import NATIVE_PROVIDER_TOOLS

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"
NAMES = ["hyperliquid_market_overview", "hyperliquid_funding_rates", "hyperliquid_orderbook"]
TITLES = {
    "hyperliquid_market_overview": "Hyperliquid Market Overview",
    "hyperliquid_funding_rates": "Hyperliquid Funding Rates",
    "hyperliquid_orderbook": "Hyperliquid Order Book",
}


def fx(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _no_net(no_network):
    yield
    assert no_network == [], f"tool touched the network: {no_network}"


@pytest.fixture
def live_markets():
    markets, delisted = svc.parse_markets(fx("meta_and_asset_ctxs.json"))
    return markets, delisted


def assert_common(res):
    assert res["exchange"] == "HYPERLIQUID"
    assert res["source"] == "hyperliquid"
    ts = res["as_of"]
    assert isinstance(ts, str)
    dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── registration ─────────────────────────────────────────────────────────────

def _tools():
    return {t.name: t for t in server.mcp._tool_manager.list_tools()}


def test_registered_names_match_hint_table():
    tools = _tools()
    assert set(NAMES) <= set(tools)
    assert set(NAMES) == set(NATIVE_PROVIDER_TOOLS["hyperliquid"])
    assert ht.TOOL_TITLES == TITLES


def test_annotations_and_async():
    tools = _tools()
    for name in NAMES:
        t = tools[name]
        a = t.annotations
        assert a.title == TITLES[name]
        assert a.readOnlyHint is True
        assert a.destructiveHint is False
        assert a.openWorldHint is True
        assert t.is_async is True
        assert inspect.iscoroutinefunction(getattr(ht, name))


def test_server_registers_exactly_the_hyperliquid_tools():
    names = {t.name for t in server.mcp._tool_manager.list_tools() if t.name.startswith("hyperliquid_")}
    assert names == set(NATIVE_PROVIDER_TOOLS["hyperliquid"])


def test_register_returns_names_in_title_order():
    from mcp.server.fastmcp import FastMCP

    local = FastMCP("t")
    assert ht.register_hyperliquid_tools(local) == NAMES
    assert {t.name for t in local._tool_manager.list_tools()} == set(NAMES)


def test_sort_keys_table():
    assert ht.SORT_KEYS == {"volume": "volume_24h_usd", "open_interest": "open_interest_usd",
                            "funding": "funding_rate_1h", "change": "change_pct_24h"}


# ── market overview ──────────────────────────────────────────────────────────

OVERVIEW_KEYS = {"exchange", "source", "as_of", "sort_by", "total_markets", "delisted_skipped",
                 "returned", "totals", "markets"}


async def test_overview_default(live_markets, hl_mock):
    markets, delisted = live_markets
    res = await ht.hyperliquid_market_overview()
    assert set(res) == OVERVIEW_KEYS
    assert_common(res)
    assert res["sort_by"] == "volume"
    vols = [m["volume_24h_usd"] for m in res["markets"]]
    assert vols == sorted(vols, reverse=True)
    assert res["total_markets"] == len(markets)
    assert res["delisted_skipped"] == len(delisted)
    assert res["returned"] == len(res["markets"]) <= 20
    names = {m["coin"] for m in res["markets"]}
    assert not names & set(delisted)
    assert set(res["totals"]) == {"volume_24h_usd", "open_interest_usd"}
    assert res["totals"]["volume_24h_usd"] == pytest.approx(round(sum(m["volume_24h_usd"] for m in markets), 2))
    assert res["totals"]["open_interest_usd"] == pytest.approx(round(sum(m["open_interest_usd"] for m in markets), 2))
    assert "BTC" in names


@pytest.mark.parametrize("sort_by,key", [
    ("volume", "volume_24h_usd"), ("open_interest", "open_interest_usd"),
    ("funding", "funding_rate_1h"), ("change", "change_pct_24h"),
])
async def test_overview_sort_orders(sort_by, key, live_markets):
    res = await ht.hyperliquid_market_overview(sort_by=sort_by, limit=100)
    values = [m[key] for m in res["markets"]]
    assert all(v is not None for v in values)
    assert values == sorted(values, reverse=True)
    if key == "change_pct_24h":
        markets, _ = live_markets
        assert res["returned"] == len([m for m in markets if m[key] is not None])


async def test_overview_sort_by_is_case_insensitive():
    res = await ht.hyperliquid_market_overview(sort_by=" Funding ")
    assert "error" not in res
    vals = [m["funding_rate_1h"] for m in res["markets"]]
    assert vals == sorted(vals, reverse=True)


async def test_overview_invalid_sort_by():
    res = await ht.hyperliquid_market_overview(sort_by="nonsense")
    e = res["error"]
    assert e["code"] == "INVALID_PARAMETER"
    assert e["valid_values"] == sorted(ht.SORT_KEYS)


async def test_overview_limit_clamping():
    assert (await ht.hyperliquid_market_overview(limit=0))["returned"] == 1
    assert (await ht.hyperliquid_market_overview(limit=-5))["returned"] == 1
    assert (await ht.hyperliquid_market_overview(limit=1000))["returned"] <= 100
    assert (await ht.hyperliquid_market_overview(limit=3))["returned"] == 3


async def test_overview_min_volume_filter(live_markets):
    markets, _ = live_markets
    vols = sorted(m["volume_24h_usd"] for m in markets)
    threshold = statistics.median(vols)
    res = await ht.hyperliquid_market_overview(min_volume_usd=threshold, limit=100)
    assert res["returned"] == len([v for v in vols if v >= threshold])
    assert all(m["volume_24h_usd"] >= threshold for m in res["markets"])
    assert res["total_markets"] == len(markets)  # counted before the volume filter
    assert res["totals"]["volume_24h_usd"] == pytest.approx(round(sum(vols), 2))


# ── funding rates ────────────────────────────────────────────────────────────

ROW_KEYS = {"coin", "funding_rate_1h", "funding_annualized_pct", "mark_price", "open_interest_usd", "volume_24h_usd"}


async def test_funding_no_symbol(live_markets):
    markets, delisted = live_markets
    res = await ht.hyperliquid_funding_rates()
    assert_common(res)
    assert res["funding_interval"] == "1h"
    assert "history" not in res and "coin" not in res and "current" not in res
    pos, neg = res["most_positive"], res["most_negative"]
    assert pos and neg, "fixture has positive and negative funding"
    assert all(r["funding_rate_1h"] > 0 for r in pos)
    assert all(r["funding_rate_1h"] < 0 for r in neg)
    assert [r["funding_rate_1h"] for r in pos] == sorted((r["funding_rate_1h"] for r in pos), reverse=True)
    assert [r["funding_rate_1h"] for r in neg] == sorted(r["funding_rate_1h"] for r in neg)
    assert len(pos) <= 10 and len(neg) <= 10
    for r in pos + neg:
        assert set(r) == ROW_KEYS
        assert r["coin"] not in delisted
    expected_pos = sorted((m for m in markets if m["funding_rate_1h"] > 0),
                          key=lambda m: m["funding_rate_1h"], reverse=True)[:10]
    assert [r["coin"] for r in pos] == [m["coin"] for m in expected_pos]


async def test_funding_limit_clamp():
    res = await ht.hyperliquid_funding_rates(limit=1)
    assert len(res["most_positive"]) == 1 and len(res["most_negative"]) == 1
    res = await ht.hyperliquid_funding_rates(limit=0)
    assert len(res["most_positive"]) == 1
    res = await ht.hyperliquid_funding_rates(limit=1000)
    assert len(res["most_positive"]) <= 50


async def test_funding_with_symbol(hl_mock, live_markets):
    markets, _ = live_markets
    res = await ht.hyperliquid_funding_rates(symbol="KPEPE")
    assert_common(res)
    assert res["coin"] == "kPEPE"
    assert [b["coin"] for b in hl_mock.of_type("fundingHistory")] == ["kPEPE"]
    kp = next(m for m in markets if m["coin"] == "kPEPE")
    assert set(res["current"]) == ROW_KEYS
    assert res["current"]["coin"] == "kPEPE"
    assert res["current"]["funding_rate_1h"] == kp["funding_rate_1h"]
    assert res["history_hours"] == 72
    expected = svc.parse_funding_history(fx("funding_history_BTC.json"))
    assert res["history"] == expected
    rates = [r["funding_rate_1h"] for r in expected]
    st = res["history_stats"]
    assert st["count"] == len(expected)
    assert st["mean_rate_1h"] == pytest.approx(sum(rates) / len(rates), rel=1e-3, abs=1e-9)
    assert st["min_rate_1h"] == pytest.approx(min(rates))
    assert st["max_rate_1h"] == pytest.approx(max(rates))
    ann = [r["funding_annualized_pct"] for r in expected]
    assert st["mean_annualized_pct"] == pytest.approx(sum(ann) / len(ann), rel=1e-3, abs=1e-6)


async def test_funding_history_hours_clamped(hl_mock):
    res = await ht.hyperliquid_funding_rates(symbol="BTC", history_hours=10_000)
    assert res["history_hours"] == 500
    (b,) = hl_mock.of_type("fundingHistory")
    assert b["endTime"] - b["startTime"] == 500 * 3_600_000
    res = await ht.hyperliquid_funding_rates(symbol="BTC", history_hours=0)
    assert res["history_hours"] == 1


async def test_funding_empty_history_stats_are_none(hl_mock):
    hl_mock.script("fundingHistory", [httpx.Response(200, json=[])])
    res = await ht.hyperliquid_funding_rates(symbol="BTC")
    assert res["history"] == []
    st = res["history_stats"]
    assert st["count"] == 0
    assert st["mean_rate_1h"] is None and st["min_rate_1h"] is None
    assert st["max_rate_1h"] is None and st["mean_annualized_pct"] is None


async def test_funding_unknown_symbol_is_envelope(hl_mock):
    res = await ht.hyperliquid_funding_rates(symbol="NOTACOIN")
    assert res["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert hl_mock.of_type("fundingHistory") == []


async def test_funding_delisted_symbol_is_envelope():
    res = await ht.hyperliquid_funding_rates(symbol="MATIC")
    assert res["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert res["error"]["delisted"] is True


# ── order book ───────────────────────────────────────────────────────────────

async def test_orderbook_btc():
    res = await ht.hyperliquid_orderbook("BTC")
    assert_common(res)
    assert res["coin"] == "BTC"
    assert res["depth"] == 10
    assert len(res["bids"]) == 10 and len(res["asks"]) == 10
    bid, ask = res["best_bid"], res["best_ask"]
    assert bid == res["bids"][0]["price"] and ask == res["asks"][0]["price"]
    assert res["spread"] == pytest.approx(ask - bid) and res["spread"] > 0
    assert res["mid"] == pytest.approx((bid + ask) / 2)
    assert res["spread_bps"] == pytest.approx(round(res["spread"] / res["mid"] * 10_000, 4), abs=1e-3)
    bid_usd = round(sum(l["price"] * l["size"] for l in res["bids"]), 2)
    ask_usd = round(sum(l["price"] * l["size"] for l in res["asks"]), 2)
    assert res["bid_depth_usd"] == pytest.approx(bid_usd, abs=0.011)
    assert res["ask_depth_usd"] == pytest.approx(ask_usd, abs=0.011)
    assert res["imbalance"] == pytest.approx(round((bid_usd - ask_usd) / (bid_usd + ask_usd), 4), abs=1e-3)
    assert -1 <= res["imbalance"] <= 1


@pytest.mark.parametrize("depth,expected", [(3, 3), (0, 1), (1000, 20)])
async def test_orderbook_depth_clamp(depth, expected):
    res = await ht.hyperliquid_orderbook("hyperliquid:btc-perp", depth=depth)
    assert res["depth"] == expected
    assert len(res["bids"]) == expected and len(res["asks"]) == expected


async def test_orderbook_unknown_symbol(hl_mock):
    res = await ht.hyperliquid_orderbook("NOTACOIN")
    assert res["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert hl_mock.of_type("l2Book") == []


async def test_orderbook_null_is_no_data(hl_mock):
    hl_mock.script("l2Book", [httpx.Response(200, content=b"null")])
    res = await ht.hyperliquid_orderbook("BTC")
    assert res["error"]["code"] == "NO_DATA"


async def test_orderbook_empty_side_is_no_data(hl_mock):
    raw = fx("l2_book_BTC.json")
    raw["levels"][1] = []
    hl_mock.script("l2Book", [httpx.Response(200, json=raw)])
    res = await ht.hyperliquid_orderbook("BTC")
    assert res["error"]["code"] == "NO_DATA"


# ── upstream failure ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda: ht.hyperliquid_market_overview(),
    lambda: ht.hyperliquid_funding_rates(),
    lambda: ht.hyperliquid_funding_rates(symbol="BTC"),
    lambda: ht.hyperliquid_orderbook("BTC"),
], ids=["overview", "funding", "funding_symbol", "orderbook"])
async def test_persistent_500_gives_retryable_envelope(hl_mock, call):
    hl_mock.script("metaAndAssetCtxs", [httpx.Response(500, text="boom")])
    res = await call()
    e = res["error"]
    assert e["code"] == "UPSTREAM_ERROR"
    assert e["retryable"] is True


async def test_tools_run_through_mcp_registry():
    tool = _tools()["hyperliquid_orderbook"]
    res = await tool.run({"symbol": "ETH", "depth": 2})
    assert res["coin"] == "ETH" and len(res["bids"]) == 2
