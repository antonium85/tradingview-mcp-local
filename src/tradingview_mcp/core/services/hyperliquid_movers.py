"""Top gainers/losers for Hyperliquid perpetuals (native API, not TradingView).

Ranked by 24h change (mark price vs previous-day price) from one
``metaAndAssetCtxs`` snapshot. Only timeframe "1D" is accepted.
"""
from __future__ import annotations

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_service as hl_svc
from tradingview_mcp.core.services import hyperliquid_symbols

MOVERS_TIMEFRAME = "1D"
MOVERS_MAX_LIMIT = 50


def check_movers_timeframe(timeframe: str) -> None:
    if (timeframe or "").strip().upper() == MOVERS_TIMEFRAME:
        return None
    raise ScreenerServiceError(
        ErrorCode.INVALID_TIMEFRAME,
        f'Hyperliquid movers are ranked by 24h change only; pass timeframe="1D" (got {timeframe!r}).',
        valid_timeframes=[MOVERS_TIMEFRAME],
        exchange="HYPERLIQUID",
        retryable=False,
    )


def build_mover_rows(markets: list[dict], *, losers: bool, limit: int) -> list[dict]:
    limit = max(1, min(int(limit), MOVERS_MAX_LIMIT))
    ranked = hl_svc.rank_markets(markets, "change_pct_24h", descending=not losers, limit=limit)
    return [
        {
            "symbol": hyperliquid_symbols.to_coinlist_line(m["coin"]),
            "changePercent": m["change_pct_24h"],
            "indicators": {
                "open": m["prev_day_price"],
                "close": m["mark_price"],
                "volume": m["volume_24h_base"],
            },
            "volume_24h_usd": m["volume_24h_usd"],
            "open_interest_usd": m["open_interest_usd"],
            "funding_rate_1h": m["funding_rate_1h"],
            "change_window": "24h",
            "exchange": "HYPERLIQUID",
            "source": "hyperliquid",
        }
        for m in ranked
    ]


def hyperliquid_top_movers(timeframe: str, *, losers: bool, limit: int) -> list[dict]:
    check_movers_timeframe(timeframe)
    snap = hl_svc.get_client().market_snapshot()
    return build_mover_rows(snap["markets"], losers=losers, limit=limit)


async def hyperliquid_top_movers_async(timeframe: str, *, losers: bool, limit: int) -> list[dict]:
    check_movers_timeframe(timeframe)
    snap = await hl_svc.get_async_client().market_snapshot()
    return build_mover_rows(snap["markets"], losers=losers, limit=limit)
