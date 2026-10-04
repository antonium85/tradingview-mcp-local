# Step 1 spec: Hyperliquid provider, symbols, routing safety, new tools

_Architect spec · 2026-10-04 · branch `local/hyperliquid` · baseline `uv run pytest tests/unit -q` = 286 passed (re-verified today)_

Source plan: `docs/hyperliquid/plan.md`. Rules: `CLAUDE.md`. Paths below are relative to the repo root; `src/` means `src/tradingview_mcp/`.

---

## 1. Scope

**In scope (Step 1):**

- **Phase 1:** the data provider `src/core/services/hyperliquid_service.py`, with a sync client and an async client that share one request builder, parsers, TTL cache, retry policy and weight budget.
- **Phase 2:** symbol normalization `src/core/services/hyperliquid_symbols.py`, plus `src/coinlist/hyperliquid.txt` and the script that generates it.
- **Phase 3:** routing safety. This covers `NATIVE_PROVIDERS`, opt-in `allow_native` validation, `ErrorCode.UNSUPPORTED_EXCHANGE`, the `pick_fallback_exchange` filter, the split hint in `symbol_not_found_error`, the `smart_money_analysis` guard, and the symbol-prefix guard (see R4).
- **Phase 5:** three new async tools: `hyperliquid_market_overview`, `hyperliquid_funding_rates` and `hyperliquid_orderbook`.
- **Phase 6 (Step 1 part):** unit tests, saved real fixtures, a routing-safety test parametrized over every tool, and one new live stress file.
- **Phase 7 (Step 1 part):** the `exchanges://list` resource and its fallback string, the FastMCP `instructions` string, and the docstrings of the new tools.

**Explicitly out of scope (Step 2 or later):**

- Phase 4. No existing tool passes `allow_native=True` in Step 1. That means no Hyperliquid branch in `top_gainers`, `top_losers`, `coin_analysis`, `multi_timeframe_analysis`, `smart_money_analysis`, the backtests or `compare_strategies`.
- Editing `backtest_service.py` or `smart_money_service.py`. Step 1 only *provides* `fetch_hyperliquid_ohlcv`, `period_to_window_ms` and `to_backtest_candles` for Step 2 to call.
- Docstring edits at `server.py:136` (top_gainers), `:190` (bollinger_scan) and the crypto list at `:739` (combined_analysis). See R9.
- Spot markets, HIP-3 perps, trading, websockets.
- `README.md`, `CHANGELOG.md`, `openclaw/`, `pyproject.toml` and dependency pins. No new dependencies: `httpx` is already a dependency, and tests mock it with `httpx.MockTransport`, not `respx`.

**Same-commit constraint.** WP-A (routing safety) and WP-E1 (`hyperliquid.txt`) must land in the **same commit**. Adding the coinlist file changes `pick_fallback_exchange` immediately. The main session commits once, at the end of the step.

---

## 2. Interfaces

### 2.1 `src/core/errors.py` (WP-A)

Add one member to `ErrorCode`, in the "Input / validation" group directly after `INVALID_EXCHANGE`:

```python
UNSUPPORTED_EXCHANGE = "UNSUPPORTED_EXCHANGE"
```

Nothing else changes.

### 2.2 `src/core/utils/validators.py` (WP-A)

New module-level names, placed after `_TA_ONLY_SCREENERS`:

```python
# Venues TradingView does not carry. NEVER add these to EXCHANGE_SCREENER or
# _TA_ONLY_SCREENERS: get_market_type() would route them to the TV scanner.
NATIVE_PROVIDERS: frozenset[str] = frozenset({"hyperliquid"})

# Tools that accept each native venue. This is the single source of truth for
# hints. Step 1 lists only the new tools; Step 2 appends opted-in tools.
NATIVE_PROVIDER_TOOLS: dict[str, tuple[str, ...]] = {
    "hyperliquid": (
        "hyperliquid_market_overview",
        "hyperliquid_funding_rates",
        "hyperliquid_orderbook",
    ),
}

def is_native_provider(exchange: str | None) -> bool
    # True iff (exchange or "").strip().lower() is in NATIVE_PROVIDERS.
    # Case- and whitespace-insensitive; None/"" give False.

def native_support_hint(exchange: str) -> str
    # Native venue: returns exactly
    #   f"{EX} is a native (non-TradingView) perpetuals venue; it is supported only by: {t1}, {t2}, {t3}."
    # where EX = exchange.strip().upper() and the tools come from NATIVE_PROVIDER_TOOLS,
    # in tuple order. Non-native venue: returns "".

def reject_native_exchange(exchange: str | None) -> None
    # Raises the UNSUPPORTED_EXCHANGE error (below) when is_native_provider(exchange).
    # Otherwise returns None and performs no other validation.
```

Changed signatures. The new parameter is keyword-only, so all 13 existing positional call sites keep working unchanged:

```python
def sanitize_exchange(ex: str, default: str = "kucoin", *, allow_native: bool = False) -> str
def validate_exchange(ex: str, default: str = "kucoin", *, allow_native: bool = False) -> str
```

`validate_exchange` behavior, in order:

1. `not ex` returns `default` unchanged, exactly as today.
2. `exs = ex.strip().lower()`. If `exs in NATIVE_PROVIDERS`:
   - with `allow_native=True`, return `exs` (`"hyperliquid"`);
   - otherwise raise:
     ```python
     ScreenerServiceError(
         ErrorCode.UNSUPPORTED_EXCHANGE,
         f"Exchange {ex!r} is not supported by this tool. {native_support_hint(exs)}",
         exchange=exs.upper(),                              # "HYPERLIQUID"
         supported_tools=list(NATIVE_PROVIDER_TOOLS[exs]),
         retryable=False,
     )
     ```
3. If `exs` is in `EXCHANGE_SCREENER` or `_TA_ONLY_SCREENERS`, return `exs`. This is unchanged.
4. Otherwise raise `INVALID_EXCHANGE` as today. `valid_exchanges` is `sorted({*EXCHANGE_SCREENER, *_TA_ONLY_SCREENERS})`, plus `NATIVE_PROVIDERS` **only when** `allow_native=True`.

`sanitize_exchange`: a native venue returns `exs` when `allow_native=True` and `default` otherwise. This keeps the legacy silent-default semantics; no tool calls `sanitize_exchange` (verified by grep). Everything else is unchanged.

`reject_native_exchange` raises the same exception as step 2 above, including the same extras.

**Symbol-prefix guard (R4).** At the very top of `normalize_tradingview_symbol(symbol, exchange)`, after `raw = (symbol or "").strip().upper()`: if `":" in raw` and `raw.split(":", 1)[0]` is a native provider, raise the same `UNSUPPORTED_EXCHANGE` error, built from the prefix. `normalize_yahoo_symbol`, `get_market_type`, `resolve_screener_for_symbol` and `get_tv_exchange_prefix` stay unchanged.

### 2.3 `src/core/services/screener_service.py` (WP-A)

Add one module constant, and extend the existing validators import to include `is_native_provider` and `native_support_hint`:

```python
_LISTING_SCAN_MAX = 64   # scan wide, filter natives, THEN apply the historical cap of 6
```

**`pick_fallback_exchange(symbol, requested_exchange) -> Optional[str]`** (currently line 550). The signature doesn't change. Only the first line of the body changes:

```python
listed = [e.upper() for e in exchanges_listing_symbol(symbol, max_results=_LISTING_SCAN_MAX)
          if not is_native_provider(e)][:6]
```

The rest of the body stays as it is. The filter must run **before** the cap of 6. Otherwise `HYPERLIQUID`, sorting early in the alphabet, could push a TradingView venue out of the top 6 and change today's fallback choice. 38 live Hyperliquid coin names collide with stock tickers in the bundled lists (`SOL`, `LINK`, `ATOM`, `OP`, ...); see R5.

**`symbol_not_found_error(symbol, exchange, **context) -> dict`** (currently line 633). The signature doesn't change. Behavior:

```python
all_listed = exchanges_listing_symbol(symbol, max_results=_LISTING_SCAN_MAX)
listed_on = [e for e in all_listed if not is_native_provider(e)][:6]
native_listed_on = [e for e in all_listed if is_native_provider(e)]
```

- If `listed_on` is non-empty, use the existing message sentence unchanged.
- If `native_listed_on` is non-empty, for each native venue append `f" {EX} also lists it, but {native_support_hint(EX)} Do not retry this tool with exchange=\"{EX}\"."`.
- If `listed_on` is empty and `native_listed_on` is non-empty, **skip** `lookup_tradingview_instrument` and **skip** the "verify the ticker spelling" sentence. The native hint is the whole suggestion.
- If both are empty, keep the current behavior exactly (network lookup, then "verify the ticker" text).
- Envelope extras:
  - `listed_on` (now excludes natives) is always present, as today.
  - `native_listed_on=[...]` and `native_supported_tools=[...]` (the union of the tool names, in order) are added **only when** `native_listed_on` is non-empty.
- `exchanges_listing_symbol` must still be called through the module-global name. Existing tests patch `screener_service.exchanges_listing_symbol`.

`coinlist.py` is **not** modified. `exchanges_listing_symbol()` keeps returning `HYPERLIQUID` and stays the error-hint source.

### 2.4 `src/server.py`. Edits are serialized: WP-A, then WP-D, then WP-E2.

| Order | Owner | Edit |
|---|---|---|
| 1 | WP-A | Add `reject_native_exchange` to the validators import block (lines 93–98). In `smart_money_analysis` (def at line 600), call `reject_native_exchange(exchange)` as the first statement inside the existing `try:`, before `return analyze_smart_money(...)` at line 631. No other handler changes. The other 13 tools already call `validate_exchange` inside a `try` and are covered by section 2.2. |
| 2 | WP-D | Add `from tradingview_mcp.hyperliquid_tools import register_hyperliquid_tools` with the service imports. Add one statement, `register_hyperliquid_tools(mcp)`, immediately above the `# ── Resource ──` banner (line 1178). This puts it above `_OFFLOADED_TOOL_COUNT = _offload_sync_tools()` (line 1252). |
| 3 | WP-E2 | Change `exchanges_list()` (line 1180), its fallback string (line 1198) and the FastMCP `instructions=(...)` string (lines 116–126) as specified in section 2.8. |

### 2.5 `src/core/services/hyperliquid_symbols.py` (WP-C, new, pure, no I/O)

```python
HYPERLIQUID_PREFIX: str = "HYPERLIQUID"
PERP_SUFFIXES: tuple[str, ...] = (".P", "-PERP", "_PERP", "/PERP", "PERP")
QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "USDH", "USDE", "USD")   # longest first
QUOTE_SEPARATORS: tuple[str, ...] = ("-", "/", "_")

def candidate_coin_names(raw: str) -> list[str]
def build_name_index(names: Iterable[str]) -> dict[str, str]
def resolve_coin(raw: str, live: Mapping[str, str], delisted: Mapping[str, str] | None = None) -> str
def to_coinlist_line(coin: str) -> str    # "kPEPE" -> "HYPERLIQUID:kPEPE"
```

**`candidate_coin_names(raw)`** returns UPPERCASE candidates, most literal first, deduplicated, order preserved:

1. `s = raw.strip()`. If `":"` is in `s`, split it into `prefix` and `rest` at the first colon. If `prefix.upper() != "HYPERLIQUID"`, raise `ScreenerServiceError(ErrorCode.INVALID_PARAMETER, ..., symbol=raw, exchange="HYPERLIQUID", retryable=False)`. The message says only `HYPERLIQUID:` or bare coin names are accepted. Otherwise `s = rest`.
2. `c0 = s.upper()`.
3. `c1 = c0` with **one** trailing `PERP_SUFFIXES` entry removed (first match in tuple order).
4. `c2 = c1` with **one** trailing `QUOTE_SUFFIXES` entry removed, then one trailing `QUOTE_SEPARATORS` char removed (so `BTC-USDC` gives `BTC`).
5. For each `c` already in the list that starts with `"1000"` and has more characters after it, also add `"K" + c[4:]`, placed right after `c`. This maps `1000PEPE` to `KPEPE` (R8).
6. Drop empty strings. If nothing is left, raise `INVALID_PARAMETER` with `symbol=raw`. This covers `""`, `"HYPERLIQUID:"` and `"-PERP"`.

**`build_name_index(names)`** returns `{name.upper(): name}`. It raises `ValueError` if two different names share the same upper-case form. None do live (verified 2026-10-04).

**`resolve_coin(raw, live, delisted)`**, where `live` and `delisted` are `build_name_index` maps:

- Return `live[c]` for the first candidate `c` found in `live`. That value is the canonical name, e.g. `"kPEPE"`.
- Otherwise, if some candidate is in `delisted`, raise `ScreenerServiceError(ErrorCode.SYMBOL_NOT_FOUND, f"{canonical} is delisted on Hyperliquid.", symbol=raw, exchange="HYPERLIQUID", delisted=True, retryable=False)`.
- Otherwise raise `ScreenerServiceError(ErrorCode.SYMBOL_NOT_FOUND, f"{raw!r} is not a listed Hyperliquid perpetual.", symbol=raw, exchange="HYPERLIQUID", suggestions=[...], retryable=False)`. `suggestions` holds at most 5 canonical names, from `difflib.get_close_matches(last_candidate, live.keys(), n=5, cutoff=0.6)` mapped back through `live`.

### 2.6 `src/core/services/hyperliquid_service.py` (WP-B, new)

#### Constants

```python
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
```

#### Environment variables

Read **at call time**, except where marked "at reset", which means the value is read at import and again on every `reset_state()`.

| Var | Default | Use |
|---|---|---|
| `TRADINGVIEW_MCP_CACHE_TTL` | `60` | TTL in seconds of the shared response cache (reused from `screener_provider`). `0` disables caching. |
| `HYPERLIQUID_MCP_RETRY_DELAYS` | `"0.5,2.0"` | Comma list of backoff delays; attempts = len + 1. ±20% jitter. `"0"` or `"0,0"` must not sleep in practice. |
| `HYPERLIQUID_MCP_TIMEOUT_S` | `10` | httpx timeout per attempt. |
| `HYPERLIQUID_MCP_MAX_INFLIGHT` | `4` | At reset: the size of the sync `threading.BoundedSemaphore` and of each per-loop `asyncio.Semaphore`. |
| `HYPERLIQUID_MCP_WEIGHT_PER_MIN` | `1000` | At reset: capacity of the shared token bucket. Hyperliquid allows 1200 per minute per IP. |

#### Test seams

These are module-level functions. Tests monkeypatch them, so the implementation must call them by module-global name:

```python
def _now_ms() -> int                      # wall clock in ms; used for windows and fetched_at_ms
def _monotonic() -> float                 # used for TTL and the token bucket
def _sleep(seconds: float) -> None        # sync backoff/throttle; wraps time.sleep
async def _async_sleep(seconds: float) -> None   # async backoff/throttle; wraps asyncio.sleep
```

#### Pure helpers

All are public, raise `ScreenerServiceError` for bad input, and do no I/O.

```python
def map_timeframe(tf: str) -> str
    # Accepts anything validators.validate_timeframe accepts ("1d", "1D", " 4H ", "1W", "1M"...)
    # and returns the TIMEFRAME_MAP value ("1D" -> "1d", "1M" -> "1M").
    # Empty or unknown raises INVALID_TIMEFRAME with valid_timeframes. Empty must NOT
    # silently default.
def interval_ms(hl_interval: str) -> int
def period_to_window_ms(period: str, *, now_ms: int | None = None) -> tuple[int, int]
    # end = (now_ms or _now_ms()) // 60_000 * 60_000
    # start = end - PERIOD_DAYS[period] * 86_400_000
    # period is stripped and lowercased. Unknown raises INVALID_PARAMETER
    # with valid_periods=sorted(PERIOD_DAYS).
def requested_bar_count(start_ms: int, end_ms: int, hl_interval: str) -> int
    # ceil((end_ms - start_ms) / interval_ms(hl_interval)), minimum 1
def request_weight(body: dict) -> int
    # 2 for type "l2Book"; 20 for every other type
def response_extra_weight(body: dict, n_items: int) -> int
    # candleSnapshot: n_items // 60; fundingHistory: n_items // 20; otherwise 0
def parse_markets(payload: Any) -> tuple[list[dict], list[str]]
    # payload = [meta, ctxs]. Returns (live_markets, delisted_names).
    # Raises UPSTREAM_ERROR (retryable=True) on a malformed shape. Ignores
    # marginTables and collateralToken.
def parse_candles(payload: Any) -> list[dict]          # ascending by "t", deduplicated on "t"
def parse_funding_history(payload: Any) -> list[dict]  # ascending by time
def parse_l2_book(payload: Any, depth: int) -> dict    # payload None raises NO_DATA (retryable=False)
def rank_markets(markets: list[dict], key: str, *, descending: bool = True,
                 limit: int | None = None) -> list[dict]
    # Sorts by markets[i][key]. Rows whose value is None are excluded. Ties keep input order.
def to_backtest_candles(candles: list[dict], hl_interval: str) -> list[dict]
```

#### Parsed shapes

All numbers are `float` unless noted. API strings are converted with `float()`; `None` stays `None`.

*Market* (one per **live** universe entry, in universe order, delisted skipped):

```python
{"coin": "kPEPE",                    # canonical name
 "mark_price": float, "prev_day_price": float | None,
 "change_pct_24h": float | None,     # round((mark/prev - 1) * 100, 4); None if prev is None or 0
 "volume_24h_usd": float,            # dayNtlVlm
 "volume_24h_base": float,           # dayBaseVlm
 "open_interest": float,             # base units, as the API gives it
 "open_interest_usd": float,         # round(open_interest * mark_price, 2)
 "funding_rate_1h": float,           # API "funding" (hourly decimal rate)
 "funding_annualized_pct": float,    # round(funding_rate_1h * 24 * 365 * 100, 4)
 "premium": float | None, "oracle_price": float | None, "mid_price": float | None,
 "max_leverage": int, "only_isolated": bool}
```

*Market snapshot*:

```python
{"markets": [Market, ...], "delisted": ["MATIC", ...], "fetched_at_ms": int}
# fetched_at_ms is when the payload came from upstream (cache insertion time),
# not when this call ran.
```

*Candle* (from `parse_candles`):

```python
{"t": int, "close_time": int, "open": float, "high": float, "low": float,
 "close": float, "volume": float, "trades": int}
```

*Candle result* (from `candles()`):

```python
{"coin": str, "interval": str, "start_ms": int, "end_ms": int,
 "requested_bars": int, "truncated": bool, "warning": str | None, "candles": [Candle, ...]}
# truncated = requested_bars > HL_MAX_CANDLES. warning is None iff not truncated.
# Otherwise warning is exactly:
# f"Hyperliquid serves only the most recent 5,000 {interval} candles; requested ~{requested_bars} bars, so older data is missing."
```

*Funding row*:

```python
{"time_ms": int, "time": str,        # ISO-8601 UTC, e.g. "2026-10-04T08:00:00Z"
 "funding_rate_1h": float, "funding_annualized_pct": float, "premium": float | None}
```

*Order book*:

```python
{"coin": str, "time_ms": int,
 "bids": [{"price": float, "size": float, "orders": int}, ...],   # best first (descending price), <= depth
 "asks": [{"price": float, "size": float, "orders": int}, ...]}   # best first (ascending price), <= depth
```

*Backtest candle* (from `to_backtest_candles`; same keys as `backtest_service._fetch_ohlcv`):

```python
{"date": str, "open": float, "high": float, "low": float, "close": float, "volume": float}
# date is UTC from "t". It uses "%Y-%m-%d" for 1d/1w/1M and "%Y-%m-%d %H:%M" for every
# other interval, matching _fetch_ohlcv for 1d and 1h.
# Prices: round(x, 4) if abs(x) >= 1, else float(f"{x:.6g}")   <- see R6
# Volume: float, not rounded
```

#### Clients

Method names are identical on both clients. Every async method is `async def`; no sync method is a coroutine function.

```python
class HyperliquidClient:                          # sync, uses httpx.Client
    def __init__(self, transport: httpx.BaseTransport | None = None, *,
                 url: str = HYPERLIQUID_INFO_URL) -> None
    def post_info(self, body: dict) -> Any        # cached + throttled + retried raw JSON
    def market_snapshot(self) -> dict             # Market snapshot
    def resolve_coin(self, symbol: str) -> str    # canonical name via hyperliquid_symbols.resolve_coin
    def get_market(self, symbol: str) -> dict     # Market for one live coin
    def candles(self, symbol: str, timeframe: str, *, bars: int | None = None,
                start_ms: int | None = None, end_ms: int | None = None) -> dict   # Candle result
    def funding_history(self, symbol: str, *, hours: int = 72) -> list[dict]
    def l2_book(self, symbol: str, *, depth: int = HL_MAX_BOOK_LEVELS) -> dict

class AsyncHyperliquidClient:                     # async, uses httpx.AsyncClient
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, *,
                 url: str = HYPERLIQUID_INFO_URL) -> None
    async def post_info(self, body: dict) -> Any
    async def market_snapshot(self) -> dict
    async def resolve_coin(self, symbol: str) -> str
    async def get_market(self, symbol: str) -> dict
    async def candles(self, symbol: str, timeframe: str, *, bars: int | None = None,
                      start_ms: int | None = None, end_ms: int | None = None) -> dict
    async def funding_history(self, symbol: str, *, hours: int = 72) -> list[dict]
    async def l2_book(self, symbol: str, *, depth: int = HL_MAX_BOOK_LEVELS) -> dict
```

**Request bodies**, built by one shared builder:

- `{"type": "metaAndAssetCtxs"}`
- `{"type": "candleSnapshot", "req": {"coin": <canonical>, "interval": <hl interval>, "startTime": int, "endTime": int}}`
- `{"type": "fundingHistory", "coin": <canonical>, "startTime": int, "endTime": int}`
- `{"type": "l2Book", "coin": <canonical>}`

Send them with `POST` and a JSON body, with header `Content-Type: application/json`. Don't configure a proxy (R13).

**Symbol resolution.** Every method that takes `symbol` resolves it first through `resolve_coin`, which uses the cached `metaAndAssetCtxs`. Only the canonical name is ever sent. An unknown or delisted symbol raises before any candle, funding or book request is made. This matters because a wrong-case coin gets HTTP 500 `null` from `candleSnapshot` and `fundingHistory` (verified).

**`candles()` window rules:**

- Passing both `bars` and `start_ms` raises `INVALID_PARAMETER`.
- If both are `None`, `bars = DEFAULT_CANDLE_BARS`.
- `bars` is clamped to `1..HL_MAX_CANDLES`, so bars-mode is never truncated.
- `end_ms` defaults to `_now_ms() // 60_000 * 60_000`.
- In bars mode, `start_ms = end_ms - bars * interval_ms`.
- `timeframe` goes through `map_timeframe`.
- An empty upstream list returns `candles: []` without raising.

**`funding_history()`.** `hours` is clamped to `1..HL_MAX_FUNDING_ROWS`. `end = _now_ms() // 60_000 * 60_000` and `startTime = end - hours * 3_600_000`. One request, no pagination.

**`l2_book()`.** `depth` is clamped to `1..HL_MAX_BOOK_LEVELS`. A `null` payload raises `NO_DATA`.

#### Shared state

One instance of each item is shared by all client instances in the process:

- **TTL cache.**
  - The key is `json.dumps(body, sort_keys=True)`. The value is `(monotonic_ts, fetched_at_ms, payload)`.
  - It is protected by a `threading.Lock` that is held only for dict operations, never across I/O or sleep.
  - Successful JSON responses are cached, except `None`. Errors are never cached.
  - Window ends are floored to the minute, so repeated candle requests within a minute share a key.
- **Weight budget** (token bucket):
  - Capacity is `HYPERLIQUID_MCP_WEIGHT_PER_MIN`, refilled at capacity/60 per second using `_monotonic()`. It starts full and is protected by a `threading.Lock`.
  - `reserve(w) -> float` returns `0.0` and deducts the weight if it is available. Otherwise it returns the seconds to wait.
  - `charge(w)` deducts and may go negative.
  - Before each attempt, both paths loop on `reserve(request_weight(body))`. Sync sleeps with `_sleep(wait)`; async sleeps with `await _async_sleep(wait)`. **The lock is never held while sleeping.**
  - After a successful response, call `charge(response_extra_weight(body, len(payload)))` when the payload is a list.
- **Concurrency.**
  - Sync uses a module `threading.BoundedSemaphore(MAX_INFLIGHT)`.
  - Async uses one `asyncio.Semaphore(MAX_INFLIGHT)` **per running event loop**: a `weakref.WeakKeyDictionary[loop, Semaphore]` guarded by a `threading.Lock`. The async path must never touch a `threading.Semaphore`, and the sync path must never touch asyncio primitives.
- **Async transport.** Create a fresh `httpx.AsyncClient(transport=self._transport, timeout=...)` per request with `async with`. Don't share an AsyncClient across calls, because pooled connections are bound to one event loop. The sync client may reuse one `httpx.Client`.

**Retry and error mapping** (identical for both paths):

| Condition | Retried? | Final `ScreenerServiceError` |
|---|---|---|
| `httpx.TimeoutException` | yes | `UPSTREAM_TIMEOUT`, `retryable=True`, `retry_after_s=10` |
| Other `httpx.TransportError` | yes | `UPSTREAM_ERROR`, `retryable=True`, `retry_after_s=30` |
| HTTP 429 | yes | `UPSTREAM_RATE_LIMIT`, `retryable=True`, `retry_after_s=int(Retry-After header)` if present and numeric, else `60` |
| HTTP 5xx | yes | `UPSTREAM_ERROR`, `retryable=True`, `retry_after_s=30`, `status_code` |
| Other HTTP 4xx (e.g. 422) | **no** | `UPSTREAM_ERROR`, `retryable=False`, `status_code` |
| 200 with a non-JSON body | yes | `UPSTREAM_ERROR`, `retryable=True` |

Every error raised by this module carries `exchange="HYPERLIQUID"`. No raw httpx exception escapes a public method.

#### Module-level API

```python
def get_client() -> HyperliquidClient              # process-wide default (lazy singleton)
def get_async_client() -> AsyncHyperliquidClient   # process-wide default (lazy singleton)
def configure_default_clients(transport: httpx.BaseTransport | None = None,
                              async_transport: httpx.AsyncBaseTransport | None = None) -> None
    # Rebuilds both singletons. If async_transport is None and transport is also an
    # httpx.AsyncBaseTransport (httpx.MockTransport is both), it is used for both.
    # Calls reset_state().
def reset_state() -> None
    # Clears the cache, rebuilds the weight budget and both semaphore pools from env.
def fetch_hyperliquid_ohlcv(symbol: str, period: str, interval: str = "1d") -> dict
    # SYNC, for Step 2 backtests. Uses get_client().
    # window = period_to_window_ms(period); hl = map_timeframe(interval)
    # Returns {"coin", "interval": hl, "period", "candles": to_backtest_candles(...),
    #          "requested_bars", "returned_bars", "truncated", "warning", "source": "hyperliquid"}
    # Zero candles raises NO_DATA (retryable=False).
```

### 2.7 `src/hyperliquid_tools.py` (WP-D, new)

This module must not import `tradingview_mcp.server`. It defines three module-level `async def` tools plus a registrar:

```python
TOOL_TITLES: dict[str, str] = {
    "hyperliquid_market_overview": "Hyperliquid Market Overview",
    "hyperliquid_funding_rates":   "Hyperliquid Funding Rates",
    "hyperliquid_orderbook":       "Hyperliquid Order Book",
}
SORT_KEYS: dict[str, str] = {"volume": "volume_24h_usd", "open_interest": "open_interest_usd",
                             "funding": "funding_rate_1h", "change": "change_pct_24h"}

def register_hyperliquid_tools(mcp: FastMCP) -> list[str]
    # For each tool:
    #   mcp.tool(annotations=ToolAnnotations(title=TOOL_TITLES[name], readOnlyHint=True,
    #            destructiveHint=False, openWorldHint=True))(fn)
    # Returns the three names in TOOL_TITLES order.
    # The set of names must equal set(NATIVE_PROVIDER_TOOLS["hyperliquid"]).

async def hyperliquid_market_overview(sort_by: str = "volume", limit: int = 20,
                                      min_volume_usd: float = 0.0) -> dict
async def hyperliquid_funding_rates(symbol: str = "", limit: int = 10,
                                    history_hours: int = 72) -> dict
async def hyperliquid_orderbook(symbol: str, depth: int = 10) -> dict
```

**Common rules for all three tools:**

- No `exchange` parameter.
- Use `get_async_client()` only. Never call the sync client or `asyncio.run`.
- Wrap the whole body in `try/except Exception` and return `exception_to_envelope(e, context=<tool name>)`. Never raise.
- Every success payload includes `"exchange": "HYPERLIQUID"`, `"source": "hyperliquid"` and `"as_of"` (an ISO-8601 UTC string from the snapshot's `fetched_at_ms`, or the book's `time_ms`).
- Docstrings must say:
  - perpetuals only, read-only, Hyperliquid's own API, not TradingView;
  - the accepted symbol forms (`BTC`, `BTCUSDT`, `BTC-PERP`, `HYPERLIQUID:BTC`, case-insensitive);
  - that `k`-prefixed coins (`kPEPE`) are 1,000-unit contracts;
  - that funding is hourly;
  - that other tools reject `exchange="HYPERLIQUID"`.

**`hyperliquid_market_overview`:**

- `limit` is clamped to `1..100`.
- If `sort_by.strip().lower()` is not in `SORT_KEYS`, return the `INVALID_PARAMETER` envelope with `valid_values=sorted(SORT_KEYS)`.
- Filter markets with `volume_24h_usd >= min_volume_usd`, then `rank_markets(..., SORT_KEYS[sort_by], descending=True, limit=limit)`.
- Returns:

```python
{"exchange", "source", "as_of", "sort_by": str, "total_markets": int,     # live count, before the volume filter
 "delisted_skipped": int, "returned": int,
 "totals": {"volume_24h_usd": float, "open_interest_usd": float},          # summed over ALL live markets, rounded to 2 dp
 "markets": [Market, ...]}
```

**`hyperliquid_funding_rates`:**

- `limit` is clamped to `1..50` and `history_hours` to `1..500`.
- A funding row in the ranking is `{"coin", "funding_rate_1h", "funding_annualized_pct", "mark_price", "open_interest_usd", "volume_24h_usd"}`.
- Returns:

```python
{"exchange", "source", "as_of", "funding_interval": "1h",
 "most_positive": [row, ...],   # rank by funding_rate_1h descending, only rows with rate > 0, <= limit
 "most_negative": [row, ...],   # ascending, only rows with rate < 0, <= limit
 # Only when symbol is non-empty:
 "coin": "kPEPE", "current": row, "history_hours": int,
 "history": [Funding row, ...],
 "history_stats": {"count": int, "mean_rate_1h": float | None, "min_rate_1h": float | None,
                   "max_rate_1h": float | None, "mean_annualized_pct": float | None}}
```

Stats are `None` when the history is empty.

**`hyperliquid_orderbook`:**

- `depth` is clamped to `1..20`.
- Returns:

```python
{"exchange", "source", "as_of", "coin", "depth": int,
 "best_bid": float, "best_ask": float, "mid": float,          # mid = (bid + ask) / 2
 "spread": float,                                            # ask - bid
 "spread_bps": float,                                        # round(spread / mid * 10_000, 4)
 "bids": [...], "asks": [...],                               # from parse_l2_book, <= depth
 "bid_depth_usd": float, "ask_depth_usd": float,             # sum(price * size) over the returned levels, rounded to 2 dp
 "imbalance": float}                                         # round((bid_usd - ask_usd) / (bid_usd + ask_usd), 4); 0.0 if both are 0
```

If either side of the book is empty, return the `NO_DATA` envelope.

### 2.8 Phase 7 metadata (WP-E2, `server.py` edit 3)

**`exchanges_list()`.** Line 1 keeps today's format (`"Available exchanges: ..."`), but excludes `NATIVE_PROVIDERS` (compare `f[:-4].lower()`). Then, for each native venue in sorted order, append `"\n" + native_support_hint(ex) + " Every other tool rejects it with UNSUPPORTED_EXCHANGE."`.

**Fallback string** (line 1198). Keep the `"Common exchanges: ..."` list unchanged, and append the same native line(s).

**`instructions=`.** Append one sentence: `"Hyperliquid perpetuals (native API, not TradingView) are available only through hyperliquid_market_overview, hyperliquid_funding_rates and hyperliquid_orderbook."`

Import `NATIVE_PROVIDERS` and `native_support_hint` from validators.

### 2.9 `src/coinlist/hyperliquid.txt` and `scripts/gen_hyperliquid_coinlist.py` (WP-E1)

The plan says `coinlist/hyperliquid.txt`; the real directory is `src/tradingview_mcp/coinlist/`. The package-data glob `coinlist/*.txt` already ships it.

The generator script, run as `uv run python scripts/gen_hyperliquid_coinlist.py`:

- POSTs `{"type": "metaAndAssetCtxs"}` with plain `httpx` and doesn't import the provider.
- Keeps universe entries **without** `isDelisted`.
- Writes one line per coin, `HYPERLIQUID:<canonical name>`, keeping the original case (`HYPERLIQUID:kPEPE`).
- Sorts by `name.upper()` and ends with a trailing newline.
- Exits non-zero if the result has fewer than 100 lines or any name fails `^[A-Za-z0-9]+$`.

There were 178 live names on 2026-10-04. **Do not** add Hyperliquid lines to `all.txt`.

---

## 3. Work packages and file ownership

| WP | Files (exclusive) | Model | Depends on | When |
|---|---|---|---|---|
| **A**: routing safety | `src/core/errors.py`, `src/core/utils/validators.py`, `src/core/services/screener_service.py`, **`src/server.py` edit 1** | **opus** | none | Wave 1. Its server.py edit is slot 1. |
| **B**: data provider | `src/core/services/hyperliquid_service.py` | **opus** | C (codes against section 2.5; integration once C lands). No dependency on A: B uses only existing error codes. | Wave 1 |
| **C**: symbols | `src/core/services/hyperliquid_symbols.py` | **sonnet** | none | Wave 1 |
| **D**: new tools | `src/hyperliquid_tools.py`, **`src/server.py` edit 2** | **sonnet** | B, C (interfaces); A (`NATIVE_PROVIDER_TOOLS` names) | Module in Wave 1. server.py slot 2, after A's edit. |
| **E1**: coinlist | `src/coinlist/hyperliquid.txt`, `scripts/gen_hyperliquid_coinlist.py` | **haiku** | none (commit together with A) | Wave 1 |
| **E2**: metadata | **`src/server.py` edit 3** (`exchanges_list`, fallback string, `instructions`) | **haiku** | A, D | server.py slot 3, last |
| **T**: tester | everything under `tests/fixtures/hyperliquid/`; `tests/unit/hyperliquid/` (`conftest.py`, `test_hl_*.py`); extensions to `tests/unit/test_strict_validation.py`, `tests/unit/test_exchange_aliases.py` and `tests/unit/test_errors.py`; new `tests/stress/test_hl_live.py` | **sonnet** | the spec only | Wave 1 (fixtures + tests), then runs the suite |

**Ownership rules:**

- **`server.py` is sequential: A, then D, then E2.** Each developer re-reads the file before editing it.
- Developers may add their own unit tests **only** in `tests/unit/hyperliquid/test_hl_dev_<wp>.py` (for example `test_hl_dev_b.py`). They must not touch the tester's files, fixtures or `conftest.py`, though they may read and use the fixtures.
- Use no `__init__.py` under `tests/unit/hyperliquid/`. All basenames there start with `test_hl_` so they stay unique under pytest's rootdir import mode.
- Nobody edits `coinlist.py`, `screener_provider.py`, `backtest_service.py`, `smart_money_service.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md` or `openclaw/`.

---

## 4. Acceptance criteria

These apply to every package:

- `uv run pytest tests/unit -q` passes with **286 + new tests, 0 failures**. The 286 existing tests must pass unmodified, apart from the tester's additive extensions.
- `uvx ruff check --isolated --select E9,F63,F7,F82 <changed .py files>` reports nothing.
- No new dependencies.

**WP-A:**

- Each of the 14 tools whose schema has an `exchange` property returns the `UNSUPPORTED_EXCHANGE` envelope for `exchange` in `{"hyperliquid", "HYPERLIQUID", " Hyperliquid "}`, with zero network I/O. The 13 that call `validate_exchange` are top_gainers, top_losers, bollinger_scan, rating_filter, coin_analysis, consecutive_candles_scan, advanced_candle_pattern, volume_breakout_scanner, volume_confirmation_analysis, smart_volume_scanner, multi_agent_analysis, multi_timeframe_analysis and combined_analysis; the 14th is smart_money_analysis.
- The envelope has `retryable is False`, `exchange == "HYPERLIQUID"`, and `supported_tools` equal to the three new tool names. Its message contains every supported tool name.
- `validate_exchange("hyperliquid", allow_native=True) == "hyperliquid"`.
- `INVALID_EXCHANGE.valid_exchanges` lists `hyperliquid` only with `allow_native=True`.
- `pick_fallback_exchange` never returns `HYPERLIQUID`. `pick_fallback_exchange("BTC", "BINANCE") is None`.
- For symbols not listed on Hyperliquid, fallback results are identical to before.
- `symbol_not_found_error` puts natives only in `native_listed_on`, never in `listed_on`, and skips the network lookup when only a native venue lists the symbol.
- A symbol prefixed `HYPERLIQUID:` passed to `coin_analysis` or `multi_agent_analysis` yields `UNSUPPORTED_EXCHANGE`, and TradingView is never called.

**WP-B:**

- Every function and shape in section 2.6 exists exactly as specified.
- Sync and async clients return equal results for the same fixtures.
- Async methods are coroutine functions; sync methods are not. The sync client works when called from inside a running event loop (no `asyncio.run`).
- Cache, weight budget and error mapping behave as specified.
- The async concurrency limit holds without blocking the loop.
- Wrong-case input never reaches the API.

**WP-C:**

- Every resolution in the test list below holds.
- Pure module: no imports of httpx or of the provider.

**WP-D:**

- Three tools are registered: async, annotated as read-only, non-destructive and open-world, with the exact titles.
- Return shapes match section 2.7.
- Every failure is an envelope.
- `test_tool_annotations.py` stays green. The tool count goes from 39 to 42.

**WP-E1:**

- The file exists, every line matches `^HYPERLIQUID:[A-Za-z0-9]+$`, and there are 100 or more lines with no case-insensitive duplicates.
- It includes `HYPERLIQUID:BTC` and `HYPERLIQUID:kPEPE`, and excludes known delisted coins (`MATIC`, `RNDR`).
- `all.txt` is unchanged.

**WP-E2:**

- The first line of `exchanges://list` doesn't name HYPERLIQUID.
- The resource output, and the fallback string, both name HYPERLIQUID, all three tools and `UNSUPPORTED_EXCHANGE`.
- `mcp.instructions` mentions the three tools.

---

## 5. Test list (tester)

Write the tests from this section, without reading the developers' code.

### 5.0 Fixtures

Capture the fixtures into `tests/fixtures/hyperliquid/` with `uv run python tests/fixtures/hyperliquid/capture_fixtures.py`. The script is owned by the tester and is not collected by pytest. It is run once, and its output is committed.

```python
"""Re-capture Hyperliquid fixtures (live network). Not a test."""
import json, pathlib, time
import httpx

URL = "https://api.hyperliquid.xyz/info"
OUT = pathlib.Path(__file__).parent

def post(body):
    r = httpx.post(URL, json=body, timeout=20)
    r.raise_for_status()
    return r.json()

def dump(name, data):
    (OUT / name).write_text(json.dumps(data, indent=1) + "\n")

now = int(time.time() * 1000) // 60_000 * 60_000
meta, ctxs = post({"type": "metaAndAssetCtxs"})
rows = list(zip(meta["universe"], ctxs))            # index-aligned; keep the pairs together
live = [r for r in rows if not r[0].get("isDelisted")]
keep = {"BTC", "ETH", "SOL", "HYPE", "kPEPE", "kBONK", "kSHIB", "MATIC", "RNDR"}
by_funding = sorted(live, key=lambda r: float(r[1]["funding"]))
keep |= {u["name"] for u, _ in by_funding[:3] + by_funding[-3:]}
def chg(r):
    p = float(r[1]["prevDayPx"] or 0)
    return float(r[1]["markPx"]) / p - 1 if p else 0.0
keep |= {u["name"] for u, _ in sorted(live, key=chg)[:3]}
sub = [r for r in rows if r[0]["name"] in keep]     # preserves universe order
assert any(u.get("isDelisted") for u, _ in sub), "need a delisted market"
assert any(u["name"].startswith("k") for u, _ in sub), "need a k-prefixed coin"
assert any(float(c["funding"]) < 0 for u, c in sub if not u.get("isDelisted")), "need negative funding"
dump("meta_and_asset_ctxs.json",
     [{"universe": [u for u, _ in sub], "marginTables": [], "collateralToken": meta.get("collateralToken")},
      [c for _, c in sub]])

def candles(coin, iv, n, ms):
    body = {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": iv, "startTime": now - n * ms, "endTime": now}}
    return post(body)[-n:]

dump("candle_snapshot_BTC_1h.json", candles("BTC", "1h", 200, 3_600_000))
dump("candle_snapshot_kPEPE_1d.json", candles("kPEPE", "1d", 60, 86_400_000))
dump("candle_snapshot_MATIC_1h_empty.json", candles("MATIC", "1h", 24, 3_600_000))   # delisted -> []
dump("funding_history_BTC.json",
     post({"type": "fundingHistory", "coin": "BTC", "startTime": now - 72 * 3_600_000})[-72:])
dump("l2_book_BTC.json", post({"type": "l2Book", "coin": "BTC"}))
dump("l2_book_unknown_null.json", post({"type": "l2Book", "coin": "NOTACOIN"}))      # -> null
dump("capture_info.json", {"captured_at_ms": now, "url": URL, "coins": sorted(keep)})
```

Each of these curl commands is equivalent to one `post()` call above:

```
curl -s -X POST https://api.hyperliquid.xyz/info -H 'Content-Type: application/json' -d '{"type":"metaAndAssetCtxs"}'
```

**Testing rules:**

- Tests must derive dynamic expectations from the fixture, not hard-code them. The top funding coins and the ranking order change between captures.
- Stable facts may be asserted directly:
  - `MATIC` and `RNDR` are delisted.
  - `kPEPE`, `kBONK` and `kSHIB` are live.
  - `KPEPE` resolves to `kPEPE`.
- Synthetic `httpx.Response` objects are allowed **only** for error paths: 429, 5xx, 422, timeouts and non-JSON bodies.

**`tests/unit/hyperliquid/conftest.py`** provides:

- a `FIXTURES` path;
- a `hl_mock` fixture that builds an `httpx.MockTransport`. It routes by request JSON `type`, and for `candleSnapshot` by `req.coin`/`req.interval`. It returns HTTP 500 with body `null` for an unknown coin, mirroring live behavior. It records every request body and the current and peak concurrency.
- an autouse fixture in that directory that sets `HYPERLIQUID_MCP_RETRY_DELAYS=0,0`, calls `hyperliquid_service.configure_default_clients(transport=mock)`, and calls `reset_state()` on teardown;
- a `no_network` fixture that monkeypatches `socket.getaddrinfo` and `socket.socket.connect` to record the call and raise.

### 5.1 Routing safety: `tests/unit/hyperliquid/test_hl_routing_safety.py` (most important)

1. **Parametrized over every tool.**
   - Discovery: `tools = [t for t in server.mcp._tool_manager.list_tools() if "exchange" in t.parameters.get("properties", {})]`, minus `NATIVE_OPT_IN: set[str] = set()`, a test-local set; Step 2 adds names to it.
   - Fill required arguments by name from a table (`symbol="BTC"`). An unknown required parameter fails with a message telling the reader to extend the table.
   - Call `await t.run({**required, "exchange": ex})` for `ex` in `["hyperliquid", "HYPERLIQUID", " Hyperliquid "]`.
   - Assert all of the following:
     - `error.code == "UNSUPPORTED_EXCHANGE"`;
     - `retryable is False`;
     - `exchange == "HYPERLIQUID"`;
     - `supported_tools == list(NATIVE_PROVIDER_TOOLS["hyperliquid"])`;
     - each tool name appears in `message`;
     - `no_network` recorded nothing;
     - the tripwires `screener_service.get_multiple_analysis`, `screener_provider._scan_with_retry`, `backtest_service._fetch_ohlcv`, `smart_money_service._fetch_ohlcv` and `server.analyze_smart_money` were never called.
2. **Discovery sanity.** There are at least 14 discovered tools, and they include `smart_money_analysis`, `combined_analysis`, `top_gainers` and `multi_timeframe_analysis`. None of the three new Hyperliquid tools has an `exchange` property.
3. **Hint names are real.** Every name in `NATIVE_PROVIDER_TOOLS["hyperliquid"]` is a registered tool.
4. **No fallback to Hyperliquid:**
   - `pick_fallback_exchange("BTC", "BINANCE") is None`.
   - For **every** coin in `hyperliquid.txt`, `pick_fallback_exchange(coin, req) != "HYPERLIQUID"` for `req` in `("BINANCE", "KUCOIN", "NASDAQ")`.
   - `pick_fallback_exchange("SOL", "NASDAQ")` is not None and not `"HYPERLIQUID"`.
5. **Filter before cap.**
   - Monkeypatch `screener_service.exchanges_listing_symbol` with a fake that honors `max_results`: `sorted({"ASX", "BIST", "BURSA", "EGX", "HKEX", "HYPERLIQUID", "KUCOIN"})[:max_results]`.
   - Assert `pick_fallback_exchange("X", "BINANCE") == "KUCOIN"`.
6. **Hint split on a TradingView miss.**
   - Set `screener_service.get_multiple_analysis` to return `{}` and `_TA_AVAILABLE` to True, and make `lookup_tradingview_instrument` raise if called.
   - Call `analyze_coin("BTC", "binance", "1h")`.
   - Expect: `SYMBOL_NOT_FOUND`; no symbol passed to `get_multiple_analysis` starts with `"HYPERLIQUID:"`; `listed_on == []`; `native_listed_on == ["HYPERLIQUID"]`; `native_supported_tools` equals the three names; the message contains `hyperliquid_market_overview`; the message does not contain "verify the ticker".
7. **Collision symbol.** `symbol_not_found_error("SOL", "NASDAQ")` (with the lookup patched to raise) gives a `listed_on` that is non-empty and lacks HYPERLIQUID, and `native_listed_on == ["HYPERLIQUID"]`.
8. **Symbol-prefix guard:**
   - `server.coin_analysis("HYPERLIQUID:BTC", exchange="KUCOIN")` and `server.multi_agent_analysis("hyperliquid:btc", exchange="KUCOIN")` return `UNSUPPORTED_EXCHANGE`, with no network and no `get_multiple_analysis` call.
   - `normalize_tradingview_symbol("HYPERLIQUID:BTC", "kucoin")` raises `ScreenerServiceError` with code `UNSUPPORTED_EXCHANGE`.
9. **Smart money stays open to other venues.** `server.smart_money_analysis("COMI", exchange="EGX")` still calls `analyze_smart_money` (monkeypatched to a sentinel).
10. **Unchanged suggestions.** `exchanges_listing_symbol("BTC")` contains `"HYPERLIQUID"`, and `exchanges_listing_symbol("kpepe") == ["HYPERLIQUID"]`. Hints still see the venue.

### 5.2 Extensions to existing upstream test files (additive only)

**`tests/unit/test_strict_validation.py`:**

- `TestValidators.test_native_exchange_rejected_by_default`: `UNSUPPORTED_EXCHANGE`, not `INVALID_EXCHANGE`.
- `test_native_exchange_allowed_with_opt_in`.
- `test_invalid_exchange_list_hides_native_unless_opt_in`: KRAKEN with and without `allow_native`.
- `test_reject_native_exchange_passes_others`: `"EGX"`, `""` and `None` return None.
- `TestToolBoundary.test_top_gainers_rejects_hyperliquid` (via `asyncio.run`).
- `test_coin_analysis_rejects_hyperliquid`.

**`tests/unit/test_exchange_aliases.py`**, new class `TestNativeProviders`:

- `"hyperliquid"` is in `NATIVE_PROVIDERS`, and not in `EXCHANGE_SCREENER`, `_TA_ONLY_SCREENERS` or `STOCK_EXCHANGES`.
- `sanitize_exchange("HYPERLIQUID", "KUCOIN") == "KUCOIN"`.
- `sanitize_exchange("HYPERLIQUID", "KUCOIN", allow_native=True) == "hyperliquid"`.
- `is_native_provider` is case- and whitespace-insensitive, and `None` gives False.
- `native_support_hint("hyperliquid")` names all three tools; `native_support_hint("binance") == ""`.

**`tests/unit/test_errors.py`:**

- `ErrorCode.UNSUPPORTED_EXCHANGE.value == "UNSUPPORTED_EXCHANGE"`, and it is distinct from `INVALID_EXCHANGE`.
- `exception_to_envelope(ScreenerServiceError(ErrorCode.UNSUPPORTED_EXCHANGE, "m", supported_tools=["a"], retryable=False))` has that shape.

**`tests/unit/test_tool_annotations.py`:** no edit. It must stay green with 42 tools.

### 5.3 Symbols: `test_hl_symbols.py`

Pure tests. The indexes are built from the fixture with `build_name_index`.

- **Resolves to `"BTC"`:** `BTC`, `btc`, `" BTC "`, `BTCUSDT`, `BTCUSDC`, `BTCUSD`, `BTC-PERP`, `BTC.P`, `BTCUSDT.P`, `BTC-USD`, `BTC/USDC`, `BTC_USDT`, `HYPERLIQUID:BTC`, `hyperliquid:btcusdc`.
- **Resolves to `"kPEPE"`:** `KPEPE`, `kpepe`, `kPEPE`, `KPEPEUSDT`, `HYPERLIQUID:KPEPE`, `1000PEPE`, `1000PEPEUSDT`. Also: every `k`-coin in the fixture resolves from its upper-case form.
- **Exact match beats suffix stripping.** With the synthetic index `build_name_index(["S", "SUSD"])`: `SUSD` resolves to `SUSD` and `SUSDT` resolves to `S`.
- **Foreign prefix.** `BINANCE:BTCUSDT` raises `INVALID_PARAMETER`.
- **Empty input.** `""`, `"HYPERLIQUID:"` and `"-PERP"` raise `INVALID_PARAMETER`.
- **Unknown coin.** `NOTACOIN` raises `SYMBOL_NOT_FOUND` with `retryable=False`, `exchange="HYPERLIQUID"`, and `suggestions` as a list of at most 5 entries.
- **Delisted coin.** `MATIC` with the delisted index raises `SYMBOL_NOT_FOUND` with `delisted=True`.
- `build_name_index(["kPEPE", "KPEPE"])` raises `ValueError`.
- `to_coinlist_line("kPEPE") == "HYPERLIQUID:kPEPE"`.

### 5.4 Provider: `test_hl_service.py`

**Mapping, windows and truncation:**

- `map_timeframe`: all 7 timeframes; the aliases `1d`, `1D`, `1W`, `1M` (stays `1M`) and `" 4H "`; `30m` and `""` raise `INVALID_TIMEFRAME`.
- `period_to_window_ms` for all 5 periods with a fixed `now_ms`. The end is floored to the minute and the span equals days × 86,400,000. `"5y"` raises `INVALID_PARAMETER` with `valid_periods`.
- **Truncation:**
  - `candles("BTC", "1h", start_ms=end - 365 days)` has `truncated=True`, and its `warning` contains "5,000".
  - `fetch_hyperliquid_ohlcv("BTC", "1y", "1h")` has `truncated=True` (8,760 bars) and a warning. `fetch_hyperliquid_ohlcv("BTC", "6mo", "1h")` has `truncated=False` (4,320 bars).
  - `fetch_hyperliquid_ohlcv("BTC", "2y", "1d")` has `truncated=False` and `warning=None`.
  - `candles(..., bars=6000)` is clamped, with `requested_bars == 5000` and `truncated=False`.
  - Passing both `bars` and `start_ms` raises `INVALID_PARAMETER`.

**Request bodies** (from the recorded requests):

- `candles("KPEPE", "1D", bars=10)` sends `coin == "kPEPE"` and `interval == "1d"`.
- `"1M"` sends `"1M"`.
- `fundingHistory` includes `startTime`, and `hours=10_000` is clamped to a 500-hour window.
- `l2Book` sends the canonical coin.
- An unknown or delisted symbol sends **no** candle, funding or book request.

**Parsers:**

- `parse_markets`: `MATIC` and `RNDR` are absent from `markets` and present in `delisted`; the market count equals the universe count minus the delisted count.
- `change_pct_24h` equals the formula. The keys match section 2.6. `None`-safe for null `premium` and `midPx`.
- `rank_markets` sorts by volume descending and by change in both directions, excludes `None`, and respects `limit`.
- `parse_candles` sorts ascending with the correct types.
- `to_backtest_candles`:
  - keys are exactly `{date, open, high, low, close, volume}`;
  - `1d` uses the `YYYY-MM-DD` date format and `1h` uses `YYYY-MM-DD HH:MM`, both in UTC;
  - a price of 85361.123456 becomes 85361.1235;
  - a kPEPE price of 0.004283 stays 0.004283 (not 0.0043).
- `parse_l2_book`: `bids[0].price < asks[0].price`, depth is clamped, and a `null` fixture raises `NO_DATA`.
- `parse_funding_history`: `funding_annualized_pct == rate * 8760 * 100`, rounded to 4 dp.

**Sync/async parity:** `market_snapshot`, `candles`, `funding_history` and `l2_book` return equal results from `HyperliquidClient(transport=m)` and `AsyncHyperliquidClient(transport=m)`.

**Sync/async discipline:**

- Every public async-client method is a coroutine function; no sync-client method is.
- An `async def` test calls `get_client().market_snapshot()` from inside the running loop, and it succeeds.

**Shared cache:**

- A sync `market_snapshot()` followed by an async `market_snapshot()` hits the transport once.
- With `TRADINGVIEW_MCP_CACHE_TTL=0`, the same sequence hits it twice.
- Different bodies are cached separately.
- After an error, the next call hits the transport again (errors are not cached).

**Retries and errors.** These run with `HYPERLIQUID_MCP_RETRY_DELAYS=0,0`, so there are 3 attempts:

| Responses | Expected |
|---|---|
| 500, 500, then 200 | succeeds, 3 calls |
| 429 three times | `UPSTREAM_RATE_LIMIT`, `retryable=True`. With a `Retry-After: 7` header, `retry_after_s == 7`. |
| `httpx.ReadTimeout` three times | `UPSTREAM_TIMEOUT` |
| 422 | 1 call only; `UPSTREAM_ERROR`, `retryable=False`, `status_code == 422` |
| 200 with a non-JSON body | `UPSTREAM_ERROR` |

Every error has `exchange == "HYPERLIQUID"`, and no httpx exception escapes.

**Async concurrency:**

- With `HYPERLIQUID_MCP_MAX_INFLIGHT=2` and `reset_state()`, start 6 concurrent async requests with distinct bodies. The async handler awaits `asyncio.sleep(0.05)`.
- Peak in-flight is at most 2.
- A concurrent ticker coroutine advances at least 3 times during the batch, which shows the loop isn't blocked.

**Shared weight budget:**

- Set `HYPERLIQUID_MCP_WEIGHT_PER_MIN=40` and call `reset_state()`.
- Monkeypatch `_monotonic` to a fake clock, and `_sleep`/`_async_sleep` to record the wait and advance the fake clock.
- One sync request plus one async request (weight 20 each, distinct bodies) cause no wait. A third request, sync or async, records a wait > 0.
- `request_weight` gives 2 for `l2Book` and 20 otherwise.
- `response_extra_weight`: candleSnapshot with 120 items gives 2; fundingHistory with 45 items gives 2.

### 5.5 Tools: `test_hl_tools.py`

These run with fixtures through `configure_default_clients` and the `no_network` fixture active.

**Registration:**

- The three names are registered, and the set equals `NATIVE_PROVIDER_TOOLS["hyperliquid"]`.
- Titles are exact. `readOnlyHint` is True, `destructiveHint` is False and `openWorldHint` is True.
- `tool.is_async` is True, and the module functions are coroutine functions.

**`hyperliquid_market_overview`:**

- The default call sorts by `volume_24h_usd` descending.
- No delisted coin appears in the results.
- `total_markets` equals the live count, and `delisted_skipped` equals the fixture's delisted count.
- `returned <= limit`, and the keys match section 2.7.
- `source` and `exchange` are correct.
- Each `sort_by` value orders correctly; for `change`, `None` values are excluded.
- An invalid `sort_by` returns an `INVALID_PARAMETER` envelope with `valid_values`.
- `limit=0` returns 1 row; `limit=1000` returns at most 100 rows.
- `min_volume_usd` filters rows.

**`hyperliquid_funding_rates`:**

- With no symbol, `most_positive` is all > 0 and descending, `most_negative` is all < 0 and ascending, there is no `history` key, and delisted coins are excluded.
- With `symbol="KPEPE"`: `coin == "kPEPE"`; the recorded request coin is `"kPEPE"`; `history` matches the fixture (BTC funding fixture routed for any coin is acceptable); `history_stats` are correct.
- An unknown symbol returns a `SYMBOL_NOT_FOUND` envelope; it doesn't raise.

**`hyperliquid_orderbook`:**

- For BTC: `spread == best_ask - best_bid > 0`, `spread_bps` matches the formula, `len(bids) == depth`, `bid_depth_usd` equals the sum, and `imbalance` is in [-1, 1].
- An unknown symbol returns `SYMBOL_NOT_FOUND`.
- When the mock returns `null` for BTC, the result is a `NO_DATA` envelope.

**Upstream failure.** Persistent 500 responses give an `UPSTREAM_ERROR` envelope with `retryable=True` for each tool, and no exception.

### 5.6 Coinlist and metadata: `test_hl_coinlist_and_docs.py`

- `hyperliquid.txt` is in `COINLIST_DIR`. Every line matches `^HYPERLIQUID:[A-Za-z0-9]+$`, and there are 100 or more lines.
- It contains `HYPERLIQUID:BTC`, `HYPERLIQUID:ETH`, `HYPERLIQUID:HYPE` and `HYPERLIQUID:kPEPE` (case preserved), has no case-insensitive duplicates, and doesn't contain `MATIC` or `RNDR`.
- `all.txt` contains no `HYPERLIQUID:` line. `load_symbols("hyperliquid")` is non-empty.
- `server.exchanges_list()`:
  - line 1 starts with `"Available exchanges:"` and lacks `HYPERLIQUID`, but still lists `KUCOIN` and `BINANCE`;
  - the full text contains `HYPERLIQUID`, all three tool names and `UNSUPPORTED_EXCHANGE`.
- **Fallback path.** Monkeypatch `server.os.path.exists` to return False. The text starts with `"Common exchanges:"`, the common list lacks HYPERLIQUID, and the text contains the native line with the three tools.
- `server.mcp.instructions` contains `hyperliquid_market_overview`.

### 5.7 Live: `tests/stress/test_hl_live.py`

This is a new file with `pytestmark = pytest.mark.stress`. It runs only on request.

- `hyperliquid_market_overview()` succeeds, and `BTC` is among the markets.
- `get_client().resolve_coin("KPEPE") == "kPEPE"`.
- `candles("BTC", "1h", bars=24)` returns 24 or 25 candles.
- `hyperliquid_funding_rates("BTC")` has a non-empty history.
- `hyperliquid_orderbook("BTC")` has `spread > 0`.
- **Coinlist drift.** At least 90% of live non-delisted names appear in `hyperliquid.txt`. If this fails, regenerate the file with `scripts/gen_hyperliquid_coinlist.py`.
- Each test must finish within 30 s.

---

## 6. Risks and decisions

Items marked **[USER]** needed a decision. **Decided 2026-10-04: R4 include the guard, R6 use the split rounding, R9 defer, all as specified below.** The others are flagged for review; the spec already applies the smallest fix.

- **R1. The plan's sync-tool rationale is partly outdated.** `server.py:1233–1252` (`_offload_sync_tools`) already runs every sync tool in `asyncio.to_thread`. Sync handlers therefore run in worker threads, not on the event loop. The plan's design still holds: a sync handler can't `await`, and `asyncio.run()` in a worker would spin up a loop per call and break any shared AsyncClient. Step 2 still needs the sync client, and the sync `threading.Semaphore` blocks only a worker. No change.
- **R2. Line drift.** The `screener_provider.py:269` semaphore is now at **:264**. All other plan references were checked and are still accurate: validators :172/:178, screener_service :550, backtest :62, and server :136/:190/:739/:1180/:1198.
- **R3. `smart_money_analysis` needs its own guard.** It takes `exchange` but never calls `validate_exchange`, and passes anything through to Yahoo. The plan's "no per-handler code" claim is therefore wrong for this one tool. The fix is a one-line `reject_native_exchange(exchange)` in server.py, owned by WP-A. Step 2 replaces it if smart money opts in.
- **R4. [USER] Symbol-prefix leak.** `coin_analysis(symbol="HYPERLIQUID:BTC", exchange="KUCOIN")` sends `HYPERLIQUID:BTC` straight to TradingView, because `normalize_tradingview_symbol` keeps any explicit prefix. The result is an error, but it does reach the TradingView scanner, which breaks the plan's guarantee. The spec adds a raise at the top of `normalize_tradingview_symbol`. It sits in `validators.py`, which WP-A already owns, and all four call sites are inside tool `try` blocks. **Default: include.** Drop section 2.2's guard and tests 5.1.8 if you'd rather not.
- **R5. Coin names collide with stock tickers.** 38 live Hyperliquid names are also tickers in nasdaq, nyse, asx or bist lists (`SOL`, `LINK`, `ATOM`, `OP`, `W`, ...). Hence filter-before-cap in `pick_fallback_exchange`, and the new `native_listed_on` / `native_supported_tools` fields on `SYMBOL_NOT_FOUND`. These extend the envelope; `listed_on` keeps its meaning of "retry here with this tool".
- **R6. [USER] Rounding for backtests.** The plan says to round OHLCV to 4 dp like `_fetch_ohlcv`. But 25 live coins trade below $0.01 (HMSTR is about $0.000174), and kPEPE at 0.004283 would become 0.0043, flattening the backtests. The spec uses 4 dp at $1 and above, and 6 significant digits below that. The keys and date format are unchanged.
- **R7. `fundingHistory` limits** (not in the plan). It returns at most 500 rows, oldest first from `startTime`, and needs `startTime`; leaving it out gives a 422. History is capped at 500 hours (about 20.8 days) with a single request and no pagination.
- **R8. More k-coins, plus a small alias.** There are 7 live k-coins, not 6 (add `kNEIRO`). The spec also maps `1000PEPE` to `kPEPE`, because Binance uses `1000PEPEUSDT`. This is a small extension beyond the plan; drop candidate rule 5 if it's unwanted.
- **R9. [USER] Phase 7 docstring edits are deferred.**
  - `server.py:136` (top_gainers) should only mention Hyperliquid once that tool opts in, in Step 2.
  - `:190` (bollinger_scan) and the `combined_analysis` crypto list at `:739` never opt in. Listing Hyperliquid there would be wrong, and the `UNSUPPORTED_EXCHANGE` envelope already lets the AI correct itself.
  - Step 1 changes only `exchanges://list`, its fallback string and `instructions`.
  - Widening `:739` to OKX or Bitget is an unrelated upstream fix, so it's left alone.
- **R10. The new tools live in a new module,** `src/hyperliquid_tools.py`, registered by one call in server.py. This keeps the rebase diff small. As a result, `server.hyperliquid_market_overview` doesn't exist; tests import from the module or use `mcp._tool_manager`.
- **R11. The live test goes in a new file,** `tests/stress/test_hl_live.py`, not `tests/stress/test_real_upstream.py`. Upstream owns that file.
- **R12. Coinlist path.** The real path is `src/tradingview_mcp/coinlist/hyperliquid.txt`, not the plan's `coinlist/hyperliquid.txt`. Delisted coins are excluded, case is preserved, and nothing is added to `all.txt`.
- **R13. Small defaults.**
  - The cache reuses `TRADINGVIEW_MCP_CACHE_TTL`.
  - New `HYPERLIQUID_MCP_*` environment variables are introduced.
  - The weight budget defaults to 1000 per minute, under Hyperliquid's 1200.
  - There's no proxy support for Hyperliquid; Yahoo's Webshare proxy isn't reused.
  - `sanitize_exchange("hyperliquid")` without opt-in silently returns the default. That's the legacy behavior, and no tool uses it.
  - `NATIVE_PROVIDERS` is a `frozenset` rather than the plan's `set`. Membership semantics are the same.
- **R14. Verified API facts the code depends on** (2026-10-04):
  - A wrong-case coin gives HTTP 500 `null` from `candleSnapshot` and `fundingHistory`, and 200 `null` from `l2Book`.
  - Interval `"1D"` gives 422; it must be `"1d"`. `"1M"` stays upper-case.
  - A delisted coin's candles come back as `[]`.
  - 234 universe entries, 56 delisted.
  - Numeric fields are strings. `premium`, `midPx` and `impactPxs` can be `null`.
