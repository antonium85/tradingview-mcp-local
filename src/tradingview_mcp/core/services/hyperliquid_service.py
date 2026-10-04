"""
Hyperliquid perpetuals data provider (native API, not TradingView).

TradingView does not carry Hyperliquid, so this module talks to Hyperliquid's
public, key-less info endpoint (``POST https://api.hyperliquid.xyz/info``)
directly. Perpetuals only, read-only.

Two parallel clients with identical method names, mirroring the sync/async
pair in ``yahoo_finance_service``:
  ``HyperliquidClient``       — sync (``httpx.Client``), for plain ``def``
                                tools and backtests running in worker threads
  ``AsyncHyperliquidClient``  — async (``httpx.AsyncClient``), for
                                ``async def`` tool handlers

Both share one request builder, the response parsers, a TTL response cache,
a weight budget (token bucket) and the retry/error-mapping policy, so only the
transport differs. Concurrency rules:
  - ``threading.Lock`` objects guard dict/bucket operations only; none is ever
    held across I/O or sleep.
  - The sync path limits in-flight requests with a ``threading.BoundedSemaphore``;
    the async path uses one ``asyncio.Semaphore`` per running event loop and
    never touches a threading semaphore.
  - The async client creates a fresh ``httpx.AsyncClient`` per request because
    pooled connections are bound to one event loop.
  - Nothing here calls ``asyncio.run``.

Every error is a ``ScreenerServiceError`` carrying ``exchange="HYPERLIQUID"``.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import threading
import time
import weakref
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_symbols

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
SOURCE = "hyperliquid"
EXCHANGE_LABEL = "HYPERLIQUID"
HL_MAX_CANDLES = 5000          # API serves only the most recent ~5,000 candles
HL_MAX_FUNDING_ROWS = 500      # fundingHistory returns <=500 rows, oldest-first from startTime
HL_MAX_BOOK_LEVELS = 20
DEFAULT_CANDLE_BARS = 300
TIMEFRAME_MAP: dict[str, str] = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h",
                                 "1D": "1d", "1W": "1w", "1M": "1M"}
INTERVAL_MS: dict[str, int] = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
                               "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
                               "1M": 2_592_000_000}   # 1M approximated as 30 days
PERIOD_DAYS: dict[str, int] = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730}

# Case-insensitive lookup; accepts exactly what validators.validate_timeframe
# accepts ("1m" means one MONTH there too, so it maps to "1M").
_TF_LOOKUP: dict[str, str] = {k.lower(): v for k, v in TIMEFRAME_MAP.items()}
_DAILY_OR_LONGER = frozenset({"1d", "1w", "1M"})
_HOURS_PER_YEAR = 24 * 365
_MINUTE_MS = 60_000
_HOUR_MS = 3_600_000
_RETRY_JITTER = 0.2            # ±20% on every backoff delay
_CACHE_MAX_ENTRIES = 256       # same cap as screener_provider; oldest evicted past it


# ─── Test seams (always called by module-global name) ───────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# ─── Errors ─────────────────────────────────────────────────────────────────


def _err(code: ErrorCode, message: str, **extra: Any) -> ScreenerServiceError:
    """Every error from this module carries exchange="HYPERLIQUID"."""
    extra.setdefault("retryable", False)
    return ScreenerServiceError(code, message, exchange=EXCHANGE_LABEL, **extra)


def _malformed(what: str, detail: Any = None) -> ScreenerServiceError:
    msg = f"Hyperliquid returned a malformed {what} payload."
    if detail is not None:
        msg += f" ({detail!r})"
    return _err(ErrorCode.UPSTREAM_ERROR, msg, retryable=True)


# ─── Environment (read at call time unless noted) ───────────────────────────


def _cache_ttl_s() -> float:
    try:
        return float(os.environ.get("TRADINGVIEW_MCP_CACHE_TTL", "60"))
    except Exception:
        return 60.0


def _retry_delays() -> tuple:
    raw = os.environ.get("HYPERLIQUID_MCP_RETRY_DELAYS", "0.5,2.0")
    try:
        return tuple(max(0.0, float(x)) for x in raw.split(",") if x.strip())
    except Exception:
        return (0.5, 2.0)


def _timeout_s() -> float:
    try:
        return max(0.1, float(os.environ.get("HYPERLIQUID_MCP_TIMEOUT_S", "10")))
    except Exception:
        return 10.0


def _max_inflight() -> int:
    # Read at reset only.
    try:
        return max(1, int(os.environ.get("HYPERLIQUID_MCP_MAX_INFLIGHT", "4")))
    except Exception:
        return 4


def _weight_per_min() -> float:
    # Read at reset only. Hyperliquid allows 1200/min per IP; stay under it.
    try:
        return max(1.0, float(os.environ.get("HYPERLIQUID_MCP_WEIGHT_PER_MIN", "1000")))
    except Exception:
        return 1000.0


def _jittered(delay: float) -> float:
    """Apply ±20% jitter so parallel callers don't synchronize retries."""
    if delay <= 0:
        return 0.0
    return max(0.0, delay * (1.0 + random.uniform(-_RETRY_JITTER, _RETRY_JITTER)))


# ─── Pure helpers ───────────────────────────────────────────────────────────


def map_timeframe(tf: str) -> str:
    """Project timeframe ("1D", " 4H ", "1M"...) -> Hyperliquid interval ("1d", "4h", "1M")."""
    key = tf.strip().lower() if isinstance(tf, str) else ""
    if key in _TF_LOOKUP:
        return _TF_LOOKUP[key]
    raise _err(
        ErrorCode.INVALID_TIMEFRAME,
        f"Invalid timeframe {tf!r}.",
        valid_timeframes=sorted(TIMEFRAME_MAP),
    )


def interval_ms(hl_interval: str) -> int:
    if hl_interval in INTERVAL_MS:
        return INTERVAL_MS[hl_interval]
    raise _err(
        ErrorCode.INVALID_TIMEFRAME,
        f"Invalid Hyperliquid interval {hl_interval!r}.",
        valid_intervals=list(INTERVAL_MS),
    )


def period_to_window_ms(period: str, *, now_ms: int | None = None) -> tuple[int, int]:
    """Yahoo-style backtest period ("1mo".."2y") -> (start_ms, end_ms), end floored to the minute."""
    key = period.strip().lower() if isinstance(period, str) else ""
    if key not in PERIOD_DAYS:
        raise _err(
            ErrorCode.INVALID_PARAMETER,
            f"Invalid period {period!r}.",
            valid_periods=sorted(PERIOD_DAYS),
        )
    end = (now_ms if now_ms is not None else _now_ms()) // _MINUTE_MS * _MINUTE_MS
    return end - PERIOD_DAYS[key] * 86_400_000, end


def requested_bar_count(start_ms: int, end_ms: int, hl_interval: str) -> int:
    step = interval_ms(hl_interval)
    return max(1, math.ceil((end_ms - start_ms) / step))


def request_weight(body: dict) -> int:
    if not isinstance(body, dict):
        raise _err(ErrorCode.INVALID_PARAMETER, "Request body must be a dict.")
    return 2 if body.get("type") == "l2Book" else 20


def response_extra_weight(body: dict, n_items: int) -> int:
    request_weight(body)  # validates
    kind = body.get("type")
    if kind == "candleSnapshot":
        return n_items // 60
    if kind == "fundingHistory":
        return n_items // 20
    return 0


def _opt_float(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _annualized_pct(rate_1h: float) -> float:
    return round(rate_1h * _HOURS_PER_YEAR * 100, 4)


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_markets(payload: Any) -> tuple[list[dict], list[str]]:
    """``[meta, ctxs]`` -> (live markets in universe order, delisted names)."""
    try:
        meta, ctxs = payload
        universe = meta["universe"]
        if not isinstance(universe, list) or not isinstance(ctxs, list) or len(universe) != len(ctxs):
            raise ValueError("universe/ctxs mismatch")
    except (TypeError, ValueError, KeyError) as e:
        raise _malformed("metaAndAssetCtxs", e) from None

    markets: list[dict] = []
    delisted: list[str] = []
    try:
        for u, c in zip(universe, ctxs):
            if u.get("isDelisted"):
                delisted.append(u["name"])
                continue
            mark = float(c["markPx"])
            prev = _opt_float(c.get("prevDayPx"))
            oi = float(c["openInterest"])
            funding = float(c["funding"])
            markets.append({
                "coin": u["name"],
                "mark_price": mark,
                "prev_day_price": prev,
                "change_pct_24h": round((mark / prev - 1) * 100, 4) if prev else None,
                "volume_24h_usd": float(c["dayNtlVlm"]),
                "volume_24h_base": float(c["dayBaseVlm"]),
                "open_interest": oi,
                "open_interest_usd": round(oi * mark, 2),
                "funding_rate_1h": funding,
                "funding_annualized_pct": _annualized_pct(funding),
                "premium": _opt_float(c.get("premium")),
                "oracle_price": _opt_float(c.get("oraclePx")),
                "mid_price": _opt_float(c.get("midPx")),
                "max_leverage": int(u.get("maxLeverage") or 0),
                "only_isolated": bool(u.get("onlyIsolated", False)),
            })
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise _malformed("metaAndAssetCtxs", e) from None
    return markets, delisted


def parse_candles(payload: Any) -> list[dict]:
    """candleSnapshot -> candles ascending by "t", deduplicated on "t" (last wins)."""
    if not isinstance(payload, list):
        raise _malformed("candleSnapshot", type(payload).__name__)
    by_t: dict[int, dict] = {}
    try:
        for c in payload:
            t = int(c["t"])
            by_t[t] = {
                "t": t,
                "close_time": int(c["T"]),
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low": float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
                "trades": int(c.get("n") or 0),
            }
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise _malformed("candleSnapshot", e) from None
    return [by_t[t] for t in sorted(by_t)]


def parse_funding_history(payload: Any) -> list[dict]:
    """fundingHistory -> rows ascending by time."""
    if not isinstance(payload, list):
        raise _malformed("fundingHistory", type(payload).__name__)
    rows: list[dict] = []
    try:
        for r in payload:
            t = int(r["time"])
            rate = float(r["fundingRate"])
            rows.append({
                "time_ms": t,
                "time": _iso_utc(t),
                "funding_rate_1h": rate,
                "funding_annualized_pct": _annualized_pct(rate),
                "premium": _opt_float(r.get("premium")),
            })
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise _malformed("fundingHistory", e) from None
    rows.sort(key=lambda r: r["time_ms"])
    return rows


def _clamp_int(value: Any, lo: int, hi: int, name: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise _err(ErrorCode.INVALID_PARAMETER, f"{name} must be an integer, got {value!r}.") from None
    return max(lo, min(hi, n))


def parse_l2_book(payload: Any, depth: int) -> dict:
    """l2Book -> best-first bids/asks, at most ``depth`` (clamped 1..20) levels each."""
    if payload is None:
        raise _err(ErrorCode.NO_DATA, "Hyperliquid returned no order book.")
    depth = _clamp_int(depth, 1, HL_MAX_BOOK_LEVELS, "depth")

    def _levels(raw: list) -> list[dict]:
        return [{"price": float(lv["px"]), "size": float(lv["sz"]), "orders": int(lv["n"])}
                for lv in raw]

    try:
        raw_bids, raw_asks = payload["levels"]
        bids = sorted(_levels(raw_bids), key=lambda lv: lv["price"], reverse=True)[:depth]
        asks = sorted(_levels(raw_asks), key=lambda lv: lv["price"])[:depth]
        return {"coin": payload["coin"], "time_ms": int(payload["time"]),
                "bids": bids, "asks": asks}
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise _malformed("l2Book", e) from None


def rank_markets(markets: list[dict], key: str, *, descending: bool = True,
                 limit: int | None = None) -> list[dict]:
    """Sort by ``key``; rows whose value is None are excluded; ties keep input order."""
    rows = [m for m in markets if m.get(key) is not None]
    # sorted() is stable for reverse=True too, so ties keep input order.
    rows = sorted(rows, key=lambda m: m[key], reverse=descending)
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return rows


def _round_price(x: float) -> float:
    # R6: 4 dp like _fetch_ohlcv at $1 and above; 6 significant digits below,
    # so sub-cent coins (kPEPE ~0.004283) don't flatten to 0.0043.
    return round(x, 4) if abs(x) >= 1 else float(f"{x:.6g}")


def to_backtest_candles(candles: list[dict], hl_interval: str) -> list[dict]:
    """Candles -> the ``backtest_service._fetch_ohlcv`` shape."""
    interval_ms(hl_interval)  # validates
    date_fmt = "%Y-%m-%d" if hl_interval in _DAILY_OR_LONGER else "%Y-%m-%d %H:%M"
    return [
        {
            "date": datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).strftime(date_fmt),
            "open": _round_price(c["open"]),
            "high": _round_price(c["high"]),
            "low": _round_price(c["low"]),
            "close": _round_price(c["close"]),
            "volume": float(c["volume"]),
        }
        for c in candles
    ]


# ─── Shared request builder ─────────────────────────────────────────────────


def _meta_body() -> dict:
    return {"type": "metaAndAssetCtxs"}


def _candles_body(coin: str, hl_interval: str, start_ms: int, end_ms: int) -> dict:
    return {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": hl_interval,
                    "startTime": int(start_ms), "endTime": int(end_ms)}}


def _funding_body(coin: str, start_ms: int, end_ms: int) -> dict:
    return {"type": "fundingHistory", "coin": coin,
            "startTime": int(start_ms), "endTime": int(end_ms)}


def _l2_body(coin: str) -> dict:
    return {"type": "l2Book", "coin": coin}


def _floor_minute(ms: int) -> int:
    return ms // _MINUTE_MS * _MINUTE_MS


def _plan_candles(timeframe: str, bars: int | None, start_ms: int | None,
                  end_ms: int | None) -> tuple[str, int, int, int]:
    """Validate and resolve the candle window -> (hl_interval, start, end, requested_bars)."""
    if bars is not None and start_ms is not None:
        raise _err(ErrorCode.INVALID_PARAMETER, "Pass either bars or start_ms, not both.")
    hl = map_timeframe(timeframe)
    step = interval_ms(hl)
    end = int(end_ms) if end_ms is not None else _floor_minute(_now_ms())
    if start_ms is None:
        n = _clamp_int(DEFAULT_CANDLE_BARS if bars is None else bars, 1, HL_MAX_CANDLES, "bars")
        return hl, end - n * step, end, n
    start = int(start_ms)
    if start >= end:
        raise _err(ErrorCode.INVALID_PARAMETER,
                   f"start_ms ({start}) must be earlier than end_ms ({end}).")
    return hl, start, end, requested_bar_count(start, end, hl)


def _candle_result(coin: str, hl: str, start: int, end: int, requested: int,
                   candles: list[dict]) -> dict:
    truncated = requested > HL_MAX_CANDLES
    warning = (
        f"Hyperliquid serves only the most recent 5,000 {hl} candles; "
        f"requested ~{requested} bars, so older data is missing."
        if truncated else None
    )
    return {"coin": coin, "interval": hl, "start_ms": start, "end_ms": end,
            "requested_bars": requested, "truncated": truncated, "warning": warning,
            "candles": candles}


def _funding_window(hours: int) -> tuple[int, int]:
    h = _clamp_int(hours, 1, HL_MAX_FUNDING_ROWS, "hours")
    end = _floor_minute(_now_ms())
    return end - h * _HOUR_MS, end


def _snapshot(fetched_at_ms: int, parsed: tuple[list[dict], list[str]]) -> dict:
    markets, delisted = parsed
    return {"markets": markets, "delisted": delisted, "fetched_at_ms": fetched_at_ms}


def _resolve_in_snapshot(symbol: str, snapshot: dict) -> str:
    try:
        live = hyperliquid_symbols.build_name_index(m["coin"] for m in snapshot["markets"])
        delisted = hyperliquid_symbols.build_name_index(snapshot["delisted"])
    except ValueError as e:
        raise _err(ErrorCode.UPSTREAM_ERROR,
                   f"Hyperliquid market list has ambiguous coin names: {e}") from None
    return hyperliquid_symbols.resolve_coin(symbol, live, delisted)


def _market_in_snapshot(symbol: str, snapshot: dict) -> dict:
    coin = _resolve_in_snapshot(symbol, snapshot)
    for m in snapshot["markets"]:
        if m["coin"] == coin:
            return m
    raise _err(ErrorCode.SYMBOL_NOT_FOUND,
               f"{coin} is not a listed Hyperliquid perpetual.", symbol=symbol)


def _book(coin: str, payload: Any, depth: int) -> dict:
    if payload is None:
        raise _err(ErrorCode.NO_DATA, f"Hyperliquid returned no order book for {coin}.",
                   symbol=coin)
    book = parse_l2_book(payload, depth)
    book["coin"] = coin  # the canonical name we resolved and requested
    return book


# ─── Shared state: TTL cache, weight budget, semaphores ─────────────────────


_CACHE: dict[str, tuple[float, int, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(body: dict) -> str:
    return json.dumps(body, sort_keys=True)


def _cache_get(key: str) -> Optional[tuple[int, Any]]:
    ttl = _cache_ttl_s()
    if ttl <= 0:
        return None
    now = _monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is None or now - entry[0] >= ttl:
        return None
    return entry[1], entry[2]


def _cache_put(key: str, payload: Any) -> int:
    """Store a successful payload; returns its fetched_at_ms. ``None`` is never cached."""
    fetched_at = _now_ms()
    ttl = _cache_ttl_s()
    if ttl <= 0 or payload is None:
        return fetched_at
    now = _monotonic()
    with _CACHE_LOCK:
        _CACHE[key] = (now, fetched_at, payload)
        if len(_CACHE) > _CACHE_MAX_ENTRIES:
            for k in [k for k, v in _CACHE.items() if now - v[0] >= ttl]:
                del _CACHE[k]
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0])]
    return fetched_at


class _WeightBudget:
    """Token bucket shared by both clients: capacity per minute, refilled continuously."""

    def __init__(self, capacity: float) -> None:
        self._capacity = float(capacity)
        self._rate = self._capacity / 60.0
        self._tokens = self._capacity
        self._ts: Optional[float] = None
        self._lock = threading.Lock()

    def _refill(self) -> None:
        # Caller holds the lock.
        now = _monotonic()
        if self._ts is not None and now > self._ts:
            self._tokens = min(self._capacity, self._tokens + (now - self._ts) * self._rate)
        self._ts = now

    def reserve(self, w: float) -> float:
        """Deduct ``w`` and return 0.0 if available, else the seconds to wait."""
        w = min(float(w), self._capacity)  # a weight above capacity would never fit
        with self._lock:
            self._refill()
            if self._tokens >= w:
                self._tokens -= w
                return 0.0
            return (w - self._tokens) / self._rate

    def charge(self, w: float) -> None:
        """Deduct unconditionally (may go negative)."""
        if w <= 0:
            return
        with self._lock:
            self._refill()
            self._tokens -= w


_BUDGET = _WeightBudget(_weight_per_min())
_MAX_INFLIGHT = _max_inflight()
_SYNC_SEMAPHORE = threading.BoundedSemaphore(_MAX_INFLIGHT)
_ASYNC_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)
_ASYNC_SEM_LOCK = threading.Lock()


def _async_semaphore() -> asyncio.Semaphore:
    """The asyncio.Semaphore for the running loop (one per loop, created lazily)."""
    loop = asyncio.get_running_loop()
    with _ASYNC_SEM_LOCK:
        sem = _ASYNC_SEMAPHORES.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(_MAX_INFLIGHT)
            _ASYNC_SEMAPHORES[loop] = sem
        return sem


def _throttle_sync(weight: int) -> None:
    while True:
        wait = _BUDGET.reserve(weight)
        if wait <= 0:
            return
        _sleep(wait)


async def _throttle_async(weight: int) -> None:
    while True:
        wait = _BUDGET.reserve(weight)
        if wait <= 0:
            return
        await _async_sleep(wait)


# ─── Shared retry / error mapping ───────────────────────────────────────────


def _decode_response(resp: httpx.Response) -> Any:
    """HTTP response -> JSON payload, or raise ScreenerServiceError (retryable flag set)."""
    status = resp.status_code
    if status == 429:
        retry_after = 60
        header = resp.headers.get("Retry-After")
        if header is not None:
            try:
                retry_after = max(0, int(float(header.strip())))
            except ValueError:
                pass
        raise _err(ErrorCode.UPSTREAM_RATE_LIMIT, "Hyperliquid rate limit (HTTP 429).",
                   retryable=True, retry_after_s=retry_after, status_code=429)
    if status >= 500:
        raise _err(ErrorCode.UPSTREAM_ERROR, f"Hyperliquid returned HTTP {status}.",
                   retryable=True, retry_after_s=30, status_code=status)
    if not resp.is_success:
        raise _err(ErrorCode.UPSTREAM_ERROR,
                   f"Hyperliquid rejected the request (HTTP {status}): {resp.text[:200]}",
                   retryable=False, status_code=status)
    try:
        return resp.json()
    except ValueError:
        raise _err(ErrorCode.UPSTREAM_ERROR, "Hyperliquid returned a non-JSON response.",
                   retryable=True, status_code=status) from None


def _transport_error(exc: httpx.HTTPError) -> ScreenerServiceError:
    if isinstance(exc, httpx.TimeoutException):
        return _err(ErrorCode.UPSTREAM_TIMEOUT, f"Hyperliquid request timed out: {exc!r}",
                    retryable=True, retry_after_s=10)
    return _err(ErrorCode.UPSTREAM_ERROR, f"Hyperliquid request failed: {exc!r}",
                retryable=True, retry_after_s=30)


def _after_success(body: dict, payload: Any) -> None:
    if isinstance(payload, list):
        _BUDGET.charge(response_extra_weight(body, len(payload)))


def _backoff_delay(attempt: int, delays: tuple) -> float:
    """Jittered delay after a failed ``attempt``, or 0.0 when no attempt follows."""
    return _jittered(delays[attempt]) if attempt < len(delays) else 0.0


def _check_body(body: Any) -> dict:
    if not isinstance(body, dict) or not body.get("type"):
        raise _err(ErrorCode.INVALID_PARAMETER, "Request body must be a dict with a 'type'.")
    return body


# ─── Clients ────────────────────────────────────────────────────────────────


_HEADERS = {"Content-Type": "application/json"}


class HyperliquidClient:
    """Sync client (``httpx.Client``). Safe to call from inside a running loop."""

    def __init__(self, transport: httpx.BaseTransport | None = None, *,
                 url: str = HYPERLIQUID_INFO_URL) -> None:
        self._url = url
        self._client = httpx.Client(transport=transport, headers=_HEADERS)

    def _fetch(self, body: dict, parse: Optional[Callable[[Any], Any]] = None) -> tuple[int, Any]:
        """(fetched_at_ms, parse(payload) or payload) — cached, throttled and retried."""
        key = _cache_key(_check_body(body))
        hit = _cache_get(key)
        if hit is not None:
            # A cached payload already parsed once, so parsing it again cannot fail.
            return hit[0], parse(hit[1]) if parse else hit[1]
        weight = request_weight(body)
        delays = _retry_delays()
        last: Optional[ScreenerServiceError] = None
        for attempt in range(len(delays) + 1):
            _throttle_sync(weight)
            sem = _SYNC_SEMAPHORE
            try:
                with sem:
                    resp = self._client.post(self._url, json=body, timeout=_timeout_s())
                payload = _decode_response(resp)
            except httpx.HTTPError as e:
                last = _transport_error(e)
            except ScreenerServiceError as e:
                last = e
            else:
                _after_success(body, payload)
                # Parse BEFORE caching: a payload that fails to parse must never be
                # served from the cache (that would pin the error for the whole TTL).
                parsed = parse(payload) if parse else payload
                return _cache_put(key, payload), parsed
            if not last.extra.get("retryable"):
                raise last
            delay = _backoff_delay(attempt, delays)
            if delay > 0:
                _sleep(delay)
        raise last

    def post_info(self, body: dict) -> Any:
        return self._fetch(body)[1]

    def market_snapshot(self) -> dict:
        return _snapshot(*self._fetch(_meta_body(), parse_markets))

    def resolve_coin(self, symbol: str) -> str:
        return _resolve_in_snapshot(symbol, self.market_snapshot())

    def get_market(self, symbol: str) -> dict:
        return _market_in_snapshot(symbol, self.market_snapshot())

    def candles(self, symbol: str, timeframe: str, *, bars: int | None = None,
                start_ms: int | None = None, end_ms: int | None = None) -> dict:
        hl, start, end, requested = _plan_candles(timeframe, bars, start_ms, end_ms)
        coin = self.resolve_coin(symbol)
        candles = self._fetch(_candles_body(coin, hl, start, end), parse_candles)[1]
        return _candle_result(coin, hl, start, end, requested, candles)

    def funding_history(self, symbol: str, *, hours: int = 72) -> list[dict]:
        start, end = _funding_window(hours)
        coin = self.resolve_coin(symbol)
        return self._fetch(_funding_body(coin, start, end), parse_funding_history)[1]

    def l2_book(self, symbol: str, *, depth: int = HL_MAX_BOOK_LEVELS) -> dict:
        depth = _clamp_int(depth, 1, HL_MAX_BOOK_LEVELS, "depth")
        coin = self.resolve_coin(symbol)
        return self._fetch(_l2_body(coin), lambda p: _book(coin, p, depth))[1]


class AsyncHyperliquidClient:
    """Async client. A fresh ``httpx.AsyncClient`` per request (loop-bound pools)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, *,
                 url: str = HYPERLIQUID_INFO_URL) -> None:
        self._url = url
        self._transport = transport

    async def _fetch(self, body: dict,
                     parse: Optional[Callable[[Any], Any]] = None) -> tuple[int, Any]:
        """(fetched_at_ms, parse(payload) or payload) — cached, throttled and retried."""
        key = _cache_key(_check_body(body))
        hit = _cache_get(key)
        if hit is not None:
            # A cached payload already parsed once, so parsing it again cannot fail.
            return hit[0], parse(hit[1]) if parse else hit[1]
        weight = request_weight(body)
        delays = _retry_delays()
        last: Optional[ScreenerServiceError] = None
        for attempt in range(len(delays) + 1):
            await _throttle_async(weight)
            sem = _async_semaphore()
            try:
                async with sem:
                    async with httpx.AsyncClient(transport=self._transport, headers=_HEADERS,
                                                 timeout=_timeout_s()) as client:
                        resp = await client.post(self._url, json=body)
                payload = _decode_response(resp)
            except httpx.HTTPError as e:
                last = _transport_error(e)
            except ScreenerServiceError as e:
                last = e
            else:
                _after_success(body, payload)
                # Parse BEFORE caching: a payload that fails to parse must never be
                # served from the cache (that would pin the error for the whole TTL).
                parsed = parse(payload) if parse else payload
                return _cache_put(key, payload), parsed
            if not last.extra.get("retryable"):
                raise last
            delay = _backoff_delay(attempt, delays)
            if delay > 0:
                await _async_sleep(delay)
        raise last

    async def post_info(self, body: dict) -> Any:
        return (await self._fetch(body))[1]

    async def market_snapshot(self) -> dict:
        return _snapshot(*(await self._fetch(_meta_body(), parse_markets)))

    async def resolve_coin(self, symbol: str) -> str:
        return _resolve_in_snapshot(symbol, await self.market_snapshot())

    async def get_market(self, symbol: str) -> dict:
        return _market_in_snapshot(symbol, await self.market_snapshot())

    async def candles(self, symbol: str, timeframe: str, *, bars: int | None = None,
                      start_ms: int | None = None, end_ms: int | None = None) -> dict:
        hl, start, end, requested = _plan_candles(timeframe, bars, start_ms, end_ms)
        coin = await self.resolve_coin(symbol)
        candles = (await self._fetch(_candles_body(coin, hl, start, end), parse_candles))[1]
        return _candle_result(coin, hl, start, end, requested, candles)

    async def funding_history(self, symbol: str, *, hours: int = 72) -> list[dict]:
        start, end = _funding_window(hours)
        coin = await self.resolve_coin(symbol)
        return (await self._fetch(_funding_body(coin, start, end), parse_funding_history))[1]

    async def l2_book(self, symbol: str, *, depth: int = HL_MAX_BOOK_LEVELS) -> dict:
        depth = _clamp_int(depth, 1, HL_MAX_BOOK_LEVELS, "depth")
        coin = await self.resolve_coin(symbol)
        return (await self._fetch(_l2_body(coin), lambda p: _book(coin, p, depth)))[1]


# ─── Module-level API ───────────────────────────────────────────────────────


_DEFAULT_CLIENT: Optional[HyperliquidClient] = None
_DEFAULT_ASYNC_CLIENT: Optional[AsyncHyperliquidClient] = None
_DEFAULTS_LOCK = threading.Lock()


def get_client() -> HyperliquidClient:
    """Process-wide default sync client (lazy singleton)."""
    global _DEFAULT_CLIENT
    with _DEFAULTS_LOCK:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = HyperliquidClient()
        return _DEFAULT_CLIENT


def get_async_client() -> AsyncHyperliquidClient:
    """Process-wide default async client (lazy singleton)."""
    global _DEFAULT_ASYNC_CLIENT
    with _DEFAULTS_LOCK:
        if _DEFAULT_ASYNC_CLIENT is None:
            _DEFAULT_ASYNC_CLIENT = AsyncHyperliquidClient()
        return _DEFAULT_ASYNC_CLIENT


def configure_default_clients(transport: httpx.BaseTransport | None = None,
                              async_transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Rebuild both singletons (tests inject ``httpx.MockTransport``), then reset state."""
    global _DEFAULT_CLIENT, _DEFAULT_ASYNC_CLIENT
    if async_transport is None and isinstance(transport, httpx.AsyncBaseTransport):
        async_transport = transport
    with _DEFAULTS_LOCK:
        old = _DEFAULT_CLIENT
        _DEFAULT_CLIENT = HyperliquidClient(transport)
        _DEFAULT_ASYNC_CLIENT = AsyncHyperliquidClient(async_transport)
    if old is not None:
        old._client.close()  # avoid leaking the pooled httpx.Client (ResourceWarning)
    reset_state()


def reset_state() -> None:
    """Clear the cache; rebuild the weight budget and both semaphore pools from env."""
    global _BUDGET, _MAX_INFLIGHT, _SYNC_SEMAPHORE, _ASYNC_SEMAPHORES
    with _CACHE_LOCK:
        _CACHE.clear()
    _BUDGET = _WeightBudget(_weight_per_min())
    _MAX_INFLIGHT = _max_inflight()
    _SYNC_SEMAPHORE = threading.BoundedSemaphore(_MAX_INFLIGHT)
    with _ASYNC_SEM_LOCK:
        _ASYNC_SEMAPHORES = weakref.WeakKeyDictionary()


def fetch_hyperliquid_ohlcv(symbol: str, period: str, interval: str = "1d") -> dict:
    """SYNC candle fetch in the ``_fetch_ohlcv`` shape, for Step 2 backtests."""
    start, end = period_to_window_ms(period)
    hl = map_timeframe(interval)
    res = get_client().candles(symbol, hl, start_ms=start, end_ms=end)
    candles = to_backtest_candles(res["candles"], hl)
    if not candles:
        raise _err(ErrorCode.NO_DATA,
                   f"Hyperliquid returned no {hl} candles for {res['coin']} over {period}.",
                   symbol=symbol, coin=res["coin"])
    return {
        "coin": res["coin"],
        "interval": hl,
        "period": period.strip().lower(),
        "candles": candles,
        "requested_bars": res["requested_bars"],
        "returned_bars": len(candles),
        "truncated": res["truncated"],
        "warning": res["warning"],
        "source": SOURCE,
    }
