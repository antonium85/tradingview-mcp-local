"""Hyperliquid perpetuals tools (native API, not TradingView).

Registered onto the FastMCP instance by ``register_hyperliquid_tools``. This
module must not import ``tradingview_mcp.server``. All tools are async and use
only the async Hyperliquid client.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tradingview_mcp.core.errors import ErrorCode, exception_to_envelope, make_error
from tradingview_mcp.core.services.hyperliquid_service import (
    EXCHANGE_LABEL,
    SOURCE,
    get_async_client,
    rank_markets,
)

TOOL_TITLES: dict[str, str] = {
    "hyperliquid_market_overview": "Hyperliquid Market Overview",
    "hyperliquid_funding_rates": "Hyperliquid Funding Rates",
    "hyperliquid_orderbook": "Hyperliquid Order Book",
}
SORT_KEYS: dict[str, str] = {
    "volume": "volume_24h_usd",
    "open_interest": "open_interest_usd",
    "funding": "funding_rate_1h",
    "change": "change_pct_24h",
}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp(value: Any, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _base(as_of_ms: int) -> dict:
    return {"exchange": EXCHANGE_LABEL, "source": SOURCE, "as_of": _iso(as_of_ms)}


def _funding_row(m: dict) -> dict:
    return {
        "coin": m["coin"],
        "funding_rate_1h": m["funding_rate_1h"],
        "funding_annualized_pct": m["funding_annualized_pct"],
        "mark_price": m["mark_price"],
        "open_interest_usd": m["open_interest_usd"],
        "volume_24h_usd": m["volume_24h_usd"],
    }


async def hyperliquid_market_overview(sort_by: str = "volume", limit: int = 20,
                                      min_volume_usd: float = 0.0) -> dict:
    """Hyperliquid perpetuals market overview, ranked.

    Perpetual futures only, read-only, from Hyperliquid's own API (not
    TradingView). Hyperliquid also works in top_gainers/top_losers (timeframe "1D"), coin_analysis, multi_timeframe_analysis, smart_money_analysis and the backtest tools (symbol "HYPERLIQUID:<COIN>"); every other tool rejects exchange="HYPERLIQUID".

    Coins whose names start with a lowercase 'k' (e.g. kPEPE) are 1,000-unit
    contracts. Funding is paid hourly (funding_rate_1h); the annualized figure
    is rate * 24 * 365 in percent. Symbol forms accepted elsewhere in this
    family of tools: BTC, BTCUSDT, BTC-PERP, HYPERLIQUID:BTC, case-insensitive.

    Args:
        sort_by: "volume", "open_interest", "funding" or "change" (descending).
        limit: Number of markets to return (1-100).
        min_volume_usd: Exclude markets with 24h notional volume below this.

    Returns:
        exchange, source, as_of, sort_by, total_markets, delisted_skipped,
        returned, totals (volume_24h_usd, open_interest_usd over all live
        markets) and markets (ranked). Errors are returned as an envelope.
    """
    try:
        key = (sort_by or "").strip().lower()
        if key not in SORT_KEYS:
            return make_error(
                ErrorCode.INVALID_PARAMETER,
                f"Invalid sort_by {sort_by!r}. Valid values: {', '.join(sorted(SORT_KEYS))}.",
                parameter="sort_by", valid_values=sorted(SORT_KEYS), retryable=False,
            )
        limit = _clamp(limit, 1, 100)
        snap = await get_async_client().market_snapshot()
        markets = snap["markets"]
        eligible = [m for m in markets if m["volume_24h_usd"] >= min_volume_usd]
        ranked = rank_markets(eligible, SORT_KEYS[key], descending=True, limit=limit)
        out = _base(snap["fetched_at_ms"])
        out.update({
            "sort_by": key,
            "total_markets": len(markets),
            "delisted_skipped": len(snap["delisted"]),
            "returned": len(ranked),
            "totals": {
                "volume_24h_usd": round(sum(m["volume_24h_usd"] for m in markets), 2),
                "open_interest_usd": round(sum(m["open_interest_usd"] for m in markets), 2),
            },
            "markets": ranked,
        })
        return out
    except Exception as e:
        return exception_to_envelope(e, context="hyperliquid_market_overview")


async def hyperliquid_funding_rates(symbol: str = "", limit: int = 10,
                                    history_hours: int = 72) -> dict:
    """Hyperliquid perpetual funding rates: extremes, plus history for one coin.

    Perpetual futures only, read-only, from Hyperliquid's own API (not
    TradingView). Hyperliquid also works in top_gainers/top_losers (timeframe "1D"), coin_analysis, multi_timeframe_analysis, smart_money_analysis and the backtest tools (symbol "HYPERLIQUID:<COIN>"); every other tool rejects exchange="HYPERLIQUID".

    Funding is hourly (funding_rate_1h is a decimal per hour; annualized is
    rate * 24 * 365 in percent). Coins starting with a lowercase 'k' (kPEPE)
    are 1,000-unit contracts.

    Args:
        symbol: Optional coin for current rate and history. Accepts BTC,
            BTCUSDT, BTC-PERP, HYPERLIQUID:BTC, case-insensitive. Empty skips.
        limit: Rows per ranking (1-50).
        history_hours: Funding history window in hours (1-500), symbol only.

    Returns:
        exchange, source, as_of, funding_interval, most_positive,
        most_negative, and when symbol is given: coin, current, history_hours,
        history and history_stats. Errors are returned as an envelope.
    """
    try:
        limit = _clamp(limit, 1, 50)
        history_hours = _clamp(history_hours, 1, 500)
        client = get_async_client()
        snap = await client.market_snapshot()
        markets = snap["markets"]
        pos = rank_markets([m for m in markets if m["funding_rate_1h"] > 0],
                           "funding_rate_1h", descending=True, limit=limit)
        neg = rank_markets([m for m in markets if m["funding_rate_1h"] < 0],
                           "funding_rate_1h", descending=False, limit=limit)
        out = _base(snap["fetched_at_ms"])
        out.update({
            "funding_interval": "1h",
            "most_positive": [_funding_row(m) for m in pos],
            "most_negative": [_funding_row(m) for m in neg],
        })
        if (symbol or "").strip():
            coin = await client.resolve_coin(symbol)
            current = next((m for m in markets if m["coin"] == coin), None)
            history = await client.funding_history(coin, hours=history_hours)
            rates = [r["funding_rate_1h"] for r in history]
            ann = [r["funding_annualized_pct"] for r in history]
            out.update({
                "coin": coin,
                "current": _funding_row(current) if current else None,
                "history_hours": history_hours,
                "history": history,
                "history_stats": {
                    "count": len(history),
                    "mean_rate_1h": sum(rates) / len(rates) if rates else None,
                    "min_rate_1h": min(rates) if rates else None,
                    "max_rate_1h": max(rates) if rates else None,
                    "mean_annualized_pct": sum(ann) / len(ann) if ann else None,
                },
            })
        return out
    except Exception as e:
        return exception_to_envelope(e, context="hyperliquid_funding_rates")


async def hyperliquid_orderbook(symbol: str, depth: int = 10) -> dict:
    """Hyperliquid perpetual L2 order book with spread and imbalance.

    Perpetual futures only, read-only, from Hyperliquid's own API (not
    TradingView). Hyperliquid also works in top_gainers/top_losers (timeframe "1D"), coin_analysis, multi_timeframe_analysis, smart_money_analysis and the backtest tools (symbol "HYPERLIQUID:<COIN>"); every other tool rejects exchange="HYPERLIQUID".

    Accepts BTC, BTCUSDT, BTC-PERP, HYPERLIQUID:BTC, case-insensitive. Coins
    starting with a lowercase 'k' (kPEPE) are 1,000-unit contracts, so sizes
    are in thousands of the underlying. Funding is hourly (see
    hyperliquid_funding_rates).

    Args:
        symbol: Coin to query.
        depth: Levels per side (1-20).

    Returns:
        exchange, source, as_of, coin, depth, best_bid, best_ask, mid,
        spread, spread_bps, bids, asks, bid_depth_usd, ask_depth_usd and
        imbalance ((bid_usd - ask_usd) / total, in -1..1). Errors are
        returned as an envelope.
    """
    try:
        depth = _clamp(depth, 1, 20)
        book = await get_async_client().l2_book(symbol, depth=depth)
        bids, asks = book["bids"], book["asks"]
        if not bids or not asks:
            return make_error(
                ErrorCode.NO_DATA,
                f"Hyperliquid order book for {book.get('coin', symbol)!r} has an empty side.",
                symbol=symbol, exchange=EXCHANGE_LABEL, retryable=False,
            )
        best_bid, best_ask = bids[0]["price"], asks[0]["price"]
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        bid_usd = sum(lv["price"] * lv["size"] for lv in bids)
        ask_usd = sum(lv["price"] * lv["size"] for lv in asks)
        total = bid_usd + ask_usd
        out = _base(book["time_ms"])
        out.update({
            "coin": book["coin"],
            "depth": depth,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "spread_bps": round(spread / mid * 10_000, 4) if mid else 0.0,
            "bids": bids,
            "asks": asks,
            "bid_depth_usd": round(bid_usd, 2),
            "ask_depth_usd": round(ask_usd, 2),
            "imbalance": round((bid_usd - ask_usd) / total, 4) if total else 0.0,
        })
        return out
    except Exception as e:
        return exception_to_envelope(e, context="hyperliquid_orderbook")


_TOOL_FUNCS = {
    "hyperliquid_market_overview": hyperliquid_market_overview,
    "hyperliquid_funding_rates": hyperliquid_funding_rates,
    "hyperliquid_orderbook": hyperliquid_orderbook,
}


def register_hyperliquid_tools(mcp: FastMCP) -> list[str]:
    """Register the three Hyperliquid tools on ``mcp``; returns their names."""
    for name, fn in _TOOL_FUNCS.items():
        mcp.tool(annotations=ToolAnnotations(
            title=TOOL_TITLES[name], readOnlyHint=True,
            destructiveHint=False, openWorldHint=True))(fn)
    return list(TOOL_TITLES)
