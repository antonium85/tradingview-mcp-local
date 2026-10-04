"""Unit tests for tradingview_mcp.hyperliquid_tools (WP-D module part)."""
import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

from tradingview_mcp.core.errors import ScreenerServiceError, ErrorCode

from tradingview_mcp import hyperliquid_tools as ht  # noqa: E402

EXPECTED = ["hyperliquid_market_overview", "hyperliquid_funding_rates", "hyperliquid_orderbook"]


def _m(coin, vol, oi, fr, chg=0.0):
    return {"coin": coin, "mark_price": 10.0, "change_pct_24h": chg, "volume_24h_usd": vol,
            "open_interest_usd": oi, "funding_rate_1h": fr,
            "funding_annualized_pct": round(fr * 24 * 365 * 100, 4)}


MARKETS = [_m("BTC", 1000.0, 500.0, 0.0001), _m("kPEPE", 50.0, 20.0, -0.0002, 3.0),
           _m("ETH", 800.0, 900.0, 0.0), _m("SOL", 10.0, 5.0, 0.0003, -1.0)]
SNAP = {"markets": MARKETS, "delisted": ["MATIC", "RNDR"], "fetched_at_ms": 1_759_564_800_000}


class FakeClient:
    def __init__(self, book=None, error=None):
        self.book = book
        self.error = error
        self.calls = []

    async def market_snapshot(self):
        if self.error:
            raise self.error
        return SNAP

    async def resolve_coin(self, symbol):
        if symbol.upper() == "KPEPE":
            return "kPEPE"
        raise ScreenerServiceError(ErrorCode.SYMBOL_NOT_FOUND, "nope", exchange="HYPERLIQUID")

    async def funding_history(self, coin, *, hours=72):
        self.calls.append(("funding", coin, hours))
        return [{"time_ms": 1, "time": "x", "funding_rate_1h": r,
                 "funding_annualized_pct": r * 8760 * 100, "premium": None}
                for r in (0.0001, 0.0003)]

    async def l2_book(self, symbol, *, depth=20):
        self.calls.append(("book", symbol, depth))
        return self.book


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(ht, "get_async_client", lambda: c)
    return c


def run(coro):
    return asyncio.run(coro)


def test_registration_annotations_and_titles():
    mcp = FastMCP("t")
    assert ht.register_hyperliquid_tools(mcp) == EXPECTED
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert set(tools) == set(EXPECTED)
    for name, t in tools.items():
        a = t.annotations
        assert a.title == ht.TOOL_TITLES[name]
        assert a.readOnlyHint is True and a.destructiveHint is False and a.openWorldHint is True
        assert "exchange" not in t.parameters.get("properties", {})
        assert asyncio.iscoroutinefunction(getattr(ht, name))


def test_names_match_native_provider_tools_when_available():
    try:
        from tradingview_mcp.core.utils.validators import NATIVE_PROVIDER_TOOLS
    except ImportError:
        pytest.skip("WP-A not landed")
    assert set(EXPECTED) <= set(NATIVE_PROVIDER_TOOLS["hyperliquid"])


def test_overview_default(client):
    r = run(ht.hyperliquid_market_overview())
    assert r["exchange"] == "HYPERLIQUID" and r["source"] == "hyperliquid"
    assert r["as_of"] == "2025-10-04T08:00:00Z"
    assert [m["coin"] for m in r["markets"]] == ["BTC", "ETH", "kPEPE", "SOL"]
    assert r["total_markets"] == 4 and r["delisted_skipped"] == 2 and r["returned"] == 4
    assert r["totals"] == {"volume_24h_usd": 1860.0, "open_interest_usd": 1425.0}


def test_overview_filter_sort_limit(client):
    r = run(ht.hyperliquid_market_overview(sort_by=" Funding ", limit=0, min_volume_usd=40))
    assert r["sort_by"] == "funding"
    assert [m["coin"] for m in r["markets"]] == ["BTC"]  # limit clamped to 1
    assert r["total_markets"] == 4  # before the volume filter
    assert r["totals"]["volume_24h_usd"] == 1860.0  # over ALL live markets


def test_overview_bad_sort(client):
    r = run(ht.hyperliquid_market_overview(sort_by="bogus"))
    assert r["error"]["code"] == "INVALID_PARAMETER"
    assert r["error"]["valid_values"] == sorted(ht.SORT_KEYS)


def test_overview_upstream_error_is_envelope(monkeypatch):
    c = FakeClient(error=ScreenerServiceError(ErrorCode.UPSTREAM_ERROR, "boom", retryable=True))
    monkeypatch.setattr(ht, "get_async_client", lambda: c)
    r = run(ht.hyperliquid_market_overview())
    assert r["error"]["code"] == "UPSTREAM_ERROR"


def test_unexpected_exception_is_envelope(monkeypatch):
    monkeypatch.setattr(ht, "get_async_client", lambda: FakeClient(error=ValueError("x")))
    r = run(ht.hyperliquid_funding_rates())
    assert "error" in r


def test_funding_no_symbol(client):
    r = run(ht.hyperliquid_funding_rates(limit=1))
    assert r["funding_interval"] == "1h"
    assert [x["coin"] for x in r["most_positive"]] == ["SOL"]
    assert [x["coin"] for x in r["most_negative"]] == ["kPEPE"]
    assert "history" not in r and "coin" not in r
    assert set(r["most_positive"][0]) == {"coin", "funding_rate_1h", "funding_annualized_pct",
                                          "mark_price", "open_interest_usd", "volume_24h_usd"}


def test_funding_zero_rate_excluded(client):
    r = run(ht.hyperliquid_funding_rates(limit=50))
    allc = [x["coin"] for x in r["most_positive"] + r["most_negative"]]
    assert "ETH" not in allc


def test_funding_with_symbol(client):
    r = run(ht.hyperliquid_funding_rates(symbol="kpepe", history_hours=9999))
    assert r["coin"] == "kPEPE" and r["current"]["coin"] == "kPEPE"
    assert r["history_hours"] == 500
    assert ("funding", "kPEPE", 500) in client.calls
    s = r["history_stats"]
    assert s["count"] == 2 and s["min_rate_1h"] == 0.0001 and s["max_rate_1h"] == 0.0003
    assert s["mean_rate_1h"] == pytest.approx(0.0002)


def test_funding_unknown_symbol(client):
    r = run(ht.hyperliquid_funding_rates(symbol="NOPE"))
    assert r["error"]["code"] == "SYMBOL_NOT_FOUND"


BOOK = {"coin": "BTC", "time_ms": 1_759_564_800_000,
        "bids": [{"price": 99.0, "size": 2.0, "orders": 1}, {"price": 98.0, "size": 1.0, "orders": 1}],
        "asks": [{"price": 101.0, "size": 1.0, "orders": 2}]}


def test_orderbook(monkeypatch):
    c = FakeClient(book=BOOK)
    monkeypatch.setattr(ht, "get_async_client", lambda: c)
    r = run(ht.hyperliquid_orderbook("btc", depth=100))
    assert ("book", "btc", 20) in c.calls and r["depth"] == 20
    assert r["best_bid"] == 99.0 and r["best_ask"] == 101.0 and r["mid"] == 100.0
    assert r["spread"] == 2.0 and r["spread_bps"] == 200.0
    assert r["bid_depth_usd"] == 296.0 and r["ask_depth_usd"] == 101.0
    assert r["imbalance"] == round((296 - 101) / 397, 4)
    assert r["as_of"] == "2025-10-04T08:00:00Z" and r["coin"] == "BTC"


def test_orderbook_empty_side(monkeypatch):
    c = FakeClient(book={**BOOK, "asks": []})
    monkeypatch.setattr(ht, "get_async_client", lambda: c)
    assert run(ht.hyperliquid_orderbook("BTC"))["error"]["code"] == "NO_DATA"
