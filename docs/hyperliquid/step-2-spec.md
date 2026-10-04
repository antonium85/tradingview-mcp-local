# Step 2 spec: route existing tools and backtests to Hyperliquid (Phase 4)

_Architect spec · 2026-10-04 · branch `local/hyperliquid` at `b1b863e` · baseline `uv run pytest tests/unit -q` = **673 passed** (re-verified today)_

Source plan: `docs/hyperliquid/plan.md` (Phase 4, "Backtests", Phase 6, Phase 7). Rules: `CLAUDE.md`. Step 1 contract: `docs/hyperliquid/step-1-spec.md`, read together with the code it produced, which differs from that spec in places. Paths are relative to the repo root; `src/` means `src/tradingview_mcp/`. Every line number below was checked against `b1b863e`.

---

## 1. Scope

**In scope (Step 2):**

- **Phase 4, tools that opt in.** Eight existing tools get a Hyperliquid branch:
  - `top_gainers` (async) and `top_losers` (sync): ranked by 24h change from `metaAndAssetCtxs`. Only `timeframe="1D"` is accepted; anything else gets `INVALID_TIMEFRAME`.
  - `coin_analysis` (sync) and `multi_timeframe_analysis` (async): indicators computed from candles with `indicators_calc.py`, with `source: "hyperliquid"`. No `Recommend.All`.
  - `backtest_strategy`, `compare_strategies` and `walk_forward_backtest_strategy` (sync): `HYPERLIQUID:`-prefixed symbols use `fetch_hyperliquid_ohlcv`, and truncation is reported in the result.
  - `smart_money_analysis` (sync): **opts in**, replacing the step-1 `reject_native_exchange` guard (decision D3).
- **Carry-overs from step 1:**
  - `combined_analysis` rejects `HYPERLIQUID:` symbols before any Marketaux call (D4).
  - `yahoo_price` rejects `HYPERLIQUID:` symbols (D5).
  - `tests/stress/test_hl_live.py` gets the reverse coinlist-drift check and a 30 s ceiling per test (D11).
- **Phase 6 (Step 2 part):** unit tests, four new candle fixtures, the routing-safety updates (`NATIVE_OPT_IN`, hint-table checks), and live tests for the opted-in tools.
- **Phase 7 (Step 2 part, R9 from step 1):**
  - docstrings of the eight opted-in tools;
  - the FastMCP `instructions` sentence, which currently says Hyperliquid is available "only through" the three new tools;
  - the three `hyperliquid_tools.py` docstrings, which currently say "Other tools reject exchange=HYPERLIQUID".

**Explicitly out of scope:**

- No new MCP tools. The tool count stays at **42** and `test_tool_annotations.py` is untouched.
- `bollinger_scan`'s docstring and the `combined_analysis` crypto list (`server.py:748`) stay as they are. Neither tool opts in, so step-1 R9 still applies.
- Step-1 minor items, deferred (D10):
  - The `hyperliquid_symbols` empty-after-strip check (`PERP`/`USD`/`USDE`). That file isn't otherwise touched, and no live coin ends in a stripped suffix (checked against all 178 lines of `hyperliquid.txt`).
  - Helpers to shrink the `exchanges_list()` and `symbol_not_found_error` diffs. `screener_service.py` isn't touched, and the change would churn a block that was just committed.
- Symbol-only tools other than `yahoo_price` (`stock_extended_hours`, the options tools, `market_sentiment`, `financial_news`, `stock_prices`). See D5.
- `hyperliquid_service.py`, `hyperliquid_symbols.py`, `screener_service.py`, `coinlist.py`, `screener_provider.py`, `errors.py`, `pyproject.toml`, `README.md`, `CHANGELOG.md`, `openclaw/`. No new dependencies.

---

## 2. Interfaces

### 2.0 Conventions that apply to every new module

- **Client discipline.** New modules import the provider as a module (`from tradingview_mcp.core.services import hyperliquid_service as hl_svc`) and call `hl_svc.get_client()`, `hl_svc.get_async_client()` and `hl_svc.fetch_hyperliquid_ohlcv(...)` **through that module attribute**. Tests rely on this: they monkeypatch `hyperliquid_service.get_client` to raise while testing an async path, and `get_async_client` to raise while testing a sync path.
- Sync functions never touch the async client, `asyncio.run` or `await`. Async functions never touch the sync client.
- Errors are raised as `ScreenerServiceError` with existing `ErrorCode`s. The server handlers already wrap everything in `exception_to_envelope`, apart from the backtest wrappers, which handle errors themselves (section 2.4).
- Coin display form is `f"HYPERLIQUID:{canonical}"`, e.g. `HYPERLIQUID:kPEPE`. This is `hyperliquid_symbols.to_coinlist_line(coin)`.
- Price rounding (`px`) follows step-1 R6: `None` stays `None`; `round(x, 4)` if `abs(x) >= 1`; otherwise `float(f"{x:.6g}")`. `sig6(x)` = `float(f"{x:.6g}")` (`None` stays `None`). Each module defines its own private helpers. Don't import `hyperliquid_service._round_price`.

### 2.1 `src/core/utils/validators.py` (WP-R)

**Changed constant.** Append eight names to the existing tuple, keeping this order:

```python
NATIVE_PROVIDER_TOOLS: dict[str, tuple[str, ...]] = {
    "hyperliquid": (
        "hyperliquid_market_overview",
        "hyperliquid_funding_rates",
        "hyperliquid_orderbook",
        "top_gainers",
        "top_losers",
        "coin_analysis",
        "multi_timeframe_analysis",
        "smart_money_analysis",
        "backtest_strategy",
        "compare_strategies",
        "walk_forward_backtest_strategy",
    ),
}
```

Update the comment above it (lines 91–92) to say that Step 2 appended the opted-in tools. `native_support_hint("hyperliquid")` therefore becomes exactly:

```
HYPERLIQUID is a native (non-TradingView) perpetuals venue; it is supported only by: hyperliquid_market_overview, hyperliquid_funding_rates, hyperliquid_orderbook, top_gainers, top_losers, coin_analysis, multi_timeframe_analysis, smart_money_analysis, backtest_strategy, compare_strategies, walk_forward_backtest_strategy.
```

`native_support_hint`'s code does not change.

**New function**, placed directly above `reject_native_symbol_prefix` (line 143):

```python
def native_symbol_prefix(symbol: str | None) -> str | None:
    """Lower-case native venue named by a "VENUE:..." prefix, else None.

    native_symbol_prefix("HYPERLIQUID:BTC")    -> "hyperliquid"
    native_symbol_prefix(" hyperliquid:kpepe ") -> "hyperliquid"
    native_symbol_prefix("BINANCE:BTCUSDT")    -> None
    native_symbol_prefix("BTC") / ("") / (None) / ("HYPERLIQUID") -> None
    """
```

Behavior: `raw = (symbol or "").strip()`. If `":"` isn't in `raw`, return `None`. Otherwise take `prefix = raw.split(":", 1)[0].strip().lower()` and return it if it is in `NATIVE_PROVIDERS`, else `None`. `reject_native_symbol_prefix` may be re-implemented on top of it, but its behavior must not change. Nothing else in validators changes.

### 2.2 `src/core/services/hyperliquid_movers.py` (WP-M, new)

```python
MOVERS_TIMEFRAME: str = "1D"
MOVERS_MAX_LIMIT: int = 50

def check_movers_timeframe(timeframe: str) -> None
def build_mover_rows(markets: list[dict], *, losers: bool, limit: int) -> list[dict]      # pure
def hyperliquid_top_movers(timeframe: str, *, losers: bool, limit: int) -> list[dict]    # SYNC
async def hyperliquid_top_movers_async(timeframe: str, *, losers: bool, limit: int) -> list[dict]  # ASYNC
```

**`check_movers_timeframe`** returns `None` when `(timeframe or "").strip().upper() == "1D"`. Otherwise it raises:

```python
ScreenerServiceError(
    ErrorCode.INVALID_TIMEFRAME,
    f'Hyperliquid movers are ranked by 24h change only; pass timeframe="1D" (got {timeframe!r}).',
    valid_timeframes=["1D"],
    exchange="HYPERLIQUID",
    retryable=False,
)
```

Both fetch functions call it **first**, before any client call, so a rejected timeframe makes zero HTTP requests.

**`build_mover_rows`:**

- `limit` is clamped to `1..MOVERS_MAX_LIMIT`.
- Rows are `hl_svc.rank_markets(markets, "change_pct_24h", descending=not losers, limit=limit)`. Markets whose change is `None` are dropped, and ties keep universe order. There is no sign filter, which matches the TradingView path. The snapshot's `markets` list already excludes delisted coins.
- Each row has exactly these keys:

```python
{"symbol": "HYPERLIQUID:kPEPE",          # to_coinlist_line(m["coin"])
 "changePercent": m["change_pct_24h"],
 "indicators": {"open": m["prev_day_price"], "close": m["mark_price"], "volume": m["volume_24h_base"]},
 "volume_24h_usd": m["volume_24h_usd"],
 "open_interest_usd": m["open_interest_usd"],
 "funding_rate_1h": m["funding_rate_1h"],
 "change_window": "24h",
 "exchange": "HYPERLIQUID",
 "source": "hyperliquid"}
```

**Fetch functions:**

- `hyperliquid_top_movers` runs `check_movers_timeframe(timeframe)`, then `snap = hl_svc.get_client().market_snapshot()`, then returns `build_mover_rows(snap["markets"], losers=losers, limit=limit)`.
- `hyperliquid_top_movers_async` is the same, using `await hl_svc.get_async_client().market_snapshot()`.
- Exactly one `metaAndAssetCtxs` request is made, or none if it's cached.

### 2.3 `src/core/services/hyperliquid_analysis.py` (WP-TA, new)

```python
TA_BARS: int = 300
MTF_TIMEFRAMES: tuple[str, ...] = ("1W", "1D", "4h", "1h", "15m")
MTF_LABELS: dict[str, str] = {           # identical to screener_service.py:1127-1133 (tf_labels)
    "1W": "Weekly (Trend Bias)", "1D": "Daily (Swing Setup)", "4h": "4-Hour (Refinement)",
    "1h": "1-Hour (Entry Timing)", "15m": "15-Min (Execution)"}
NOT_AVAILABLE: tuple[str, ...] = ("Recommend.All", "Recommend.MA", "Recommend.Other", "ADX",
    "Stochastic", "VWAP", "Pivot support/resistance", "CCI", "Williams %R", "Ichimoku", "Parabolic SAR")
NOTE: str = ("Computed from Hyperliquid candles with this server's own indicator code; "
             "TradingView ratings (Recommend.All) are not available for this venue.")

def indicator_snapshot(candles: list[dict]) -> dict                                   # pure
def build_coin_analysis(market: dict, timeframe: str, candle_result: dict) -> dict    # pure
def build_tf_entry(timeframe: str, candles: list[dict]) -> tuple[dict, int]           # pure
def build_alignment(alignment_scores: list[tuple[str, int]]) -> tuple[dict, dict]     # pure
def analyze_hyperliquid_coin(symbol: str, timeframe: str) -> dict                      # SYNC
async def analyze_hyperliquid_mtf(symbol: str) -> dict                                 # ASYNC
```

`candles` here means `parse_candles` output: ascending dicts with `t, open, high, low, close, volume`. The last bar is the in-progress one and is **included**, as TradingView's real-time values are.

**`indicator_snapshot(candles)`** requires `len(candles) >= 1`; an empty list raises `ScreenerServiceError(NO_DATA, ..., exchange="HYPERLIQUID", retryable=False)`. It returns a dict keyed with TradingView names. Every key is always present, with unrounded `float` values, or `None` when there is too little history:

| Key(s) | Source |
|---|---|
| `open`, `high`, `low`, `close`, `volume` | last candle |
| `RSI` | `calc_rsi(closes, 14)[-1]` |
| `MACD.macd`, `MACD.signal` | `calc_macd(closes, 12, 26, 9)` last values |
| `EMA9`, `EMA10`, `EMA20`, `EMA30`, `EMA50`, `EMA100`, `EMA200` | `calc_ema(closes, n)[-1]` |
| `SMA10`, `SMA20`, `SMA30`, `SMA50`, `SMA100`, `SMA200` | `calc_sma(closes, n)[-1]` |
| `BB.upper`, `BB.lower` | `calc_bollinger(closes, 20, 2.0)` last values (the middle is `SMA20`) |
| `ATR` | `calc_atr(highs, lows, closes, 14)[-1]` |
| `volume.SMA20` | `calc_sma(volumes, 20)[-1]` |
| `Supertrend.dir`, `Supertrend.upper`, `Supertrend.lower` | `calc_supertrend(highs, lows, closes, 10, 3.0)` last values (`dir` is `1`, `-1` or `None`) |

**`build_coin_analysis(market, timeframe, candle_result)`** takes:

- `market`: a step-1 *Market* dict;
- `timeframe`: the validated TradingView form, e.g. `"1h"` or `"1D"`;
- `candle_result`: a step-1 *Candle result*.

It builds `s = indicator_snapshot(candle_result["candles"])` and returns exactly these top-level keys:

```python
{"symbol": "HYPERLIQUID:BTC", "coin": "BTC", "exchange": "HYPERLIQUID", "source": "hyperliquid",
 "timeframe": timeframe, "interval": candle_result["interval"], "bars_used": len(candles),
 "last_bar_time": "YYYY-MM-DDTHH:MM:SSZ",              # UTC, from the last candle's "t"
 "price_data": {"current_price": px(close), "open": px, "high": px, "low": px, "close": px,
                "change_percent": round((close - open) / open * 100, 3) if open else 0.0,
                "volume": round(volume, 4)},
 "timeframe_context": indicators.analyze_timeframe_context(s, timeframe),   # reused as-is
 "rsi": {...}, "macd": {...}, "sma": {...}, "ema": {...}, "bollinger_bands": {...},
 "atr": {...}, "volume_analysis": {...}, "supertrend": {...}, "market_structure": {...},
 "market_sentiment": {...}, "perp_context": {...},
 "not_available": list(NOT_AVAILABLE), "note": NOTE}
```

The sections are built **by this module**. Don't call `extract_extended_indicators`, which rounds everything to 4 dp and emits `tv_recommendation`. Labels and thresholds copy `indicators.py` exactly:

- `rsi`: `{"value": round(RSI, 2) | None, "signal": ...}`. The signal rules are those in `indicators.py:89-100`: above 70 Overbought, above 60 Bullish, below 30 Oversold, below 40 Bearish, otherwise (or `None`) Neutral.
- `macd`: `{"macd_line": sig6, "signal_line": sig6, "histogram": sig6(macd - signal), "crossover": "Bullish" | "Bearish" | "Neutral"}`, following `indicators.py:223-240`.
- `sma`: `{"sma10", "sma20", "sma30", "sma50", "sma100", "sma200"}`, all `px`.
- `ema`: `{"ema9", "ema10", "ema20", "ema30", "ema50", "ema100", "ema200"}`, all `px`.
- `bollinger_bands`: `{"upper", "middle", "lower"}` (all `px`), plus `"width"` (`round(bbw, 4)` or `None`), `"squeeze"` (`bbw < 0.02`; `False` when unknown) and `"position"`. Position follows `indicators.py:266-295`: Above Upper Band, Below Lower Band, Upper Half, Lower Half, or Unknown.
- `atr`: `{"value": px, "percent_of_price": round(atr / close * 100, 2) | None, "volatility": ...}`. Volatility follows `indicators.py:204-221`: Unknown, High above 3, Medium above 1.5, otherwise Low.
- `volume_analysis`: `{"current": round(v, 4), "average_20": round(vsma, 4) | None, "ratio": round(v / vsma, 2) | None, "signal": ...}`. The signal follows `indicators.py:242-264`: at least 3 Very High, at least 2 High, at least 1.5 Above Average, below 0.5 Very Low, below 0.8 Below Average, otherwise Normal.
- `supertrend`: `{"direction": "Bullish" (1) | "Bearish" (-1) | None, "upper": px, "lower": px}`.
- `market_structure`: `{"trend": ..., "trend_score": int}`. The score adds +1 or -1 for close above or below each of EMA20, EMA50 and EMA200, skipping any that are `None`. A score of 2 or more is Bullish, -2 or less is Bearish, and anything else is `"Neutral/Ranging"`. There's no `trend_strength`, because ADX isn't computed.
- `market_sentiment`: the same keys as the TradingView `coin_analysis` (`screener_service.py:851-862`), computed from Bollinger values, never from `Recommend.All`:
  - `"overall_rating"` and `"buy_sell_signal"` come from `indicators.compute_bb_rating_signal(close, BB.upper, SMA20, BB.lower)`, or `None` and `"N/A"` when the bands are unavailable;
  - `"volatility"` is from bbw: `"Unknown"` if `None`, High above 0.05, Medium above 0.02, otherwise Low;
  - `"momentum"` is `"Bullish"` if `change_percent > 0`, else `"Bearish"`.
- `perp_context`: `{k: market[k] for k in ("mark_price", "prev_day_price", "change_pct_24h", "funding_rate_1h", "funding_annualized_pct", "open_interest_usd", "volume_24h_usd")}`.

**`build_tf_entry(timeframe, candles)`** returns `(entry, bias_num)`, where `bias_num` is `1`, `-1` or `0` from `analyze_timeframe_context(s, timeframe)["bias"]`. The entry has exactly these keys:

```python
{"label": MTF_LABELS[timeframe], "bias", "bias_reasons", "key_indicators", "advice",   # from timeframe_context
 "price": px(close), "change_pct": <as price_data.change_percent>,
 "rsi": <rsi section>, "macd_crossover": <macd.crossover>,
 "ema_trend": {"ema20": px, "ema50": px, "ema200": px},
 "volume_signal": <volume_analysis.signal>, "market_structure": <market_structure.trend>,
 "supertrend": <supertrend.direction>, "bars_used": int}
```

`key_indicators` is `timeframe_context["key_indicators_for_timeframe"]`.

**`build_alignment(alignment_scores)`** returns `(alignment, recommendation)`. It copies the rules and strings of `screener_service.run_multi_timeframe_analysis` verbatim: status, confidence and action from lines 1237–1254, `divergent_timeframes` from lines 1256–1261, and `recommendation` (action, entry_timeframe and the five rules) from lines 1275–1285. The keys are:

- `alignment = {"status", "confidence", "net_score", "scores_by_tf": dict(alignment_scores), "divergent_timeframes"}`
- `recommendation = {"action", "entry_timeframe", "rules"}`

**`analyze_hyperliquid_coin(symbol, timeframe)`** (SYNC):

1. `client = hl_svc.get_client()`.
2. `market = client.get_market(symbol)`. This resolves the symbol and raises `SYMBOL_NOT_FOUND` (with `delisted=True` where it applies) or `INVALID_PARAMETER` before any candle request.
3. `res = client.candles(market["coin"], timeframe, bars=TA_BARS)`.
4. If `res["candles"]` is empty, raise `NO_DATA` (`exchange="HYPERLIQUID"`, `retryable=False`).
5. Return `build_coin_analysis(market, timeframe, res)`.

**`analyze_hyperliquid_mtf(symbol)`** (ASYNC):

1. `client = hl_svc.get_async_client()`, then `coin = await client.resolve_coin(symbol)`. Resolution errors propagate.
2. Fetch all five timeframes concurrently with `asyncio.gather(*(client.candles(coin, tf, bars=TA_BARS) for tf in MTF_TIMEFRAMES), return_exceptions=True)`. The async client's own semaphore bounds how many are in flight.
3. For each timeframe in `MTF_TIMEFRAMES` order:
   - an exception gives `{"error": str(exc)}`;
   - empty candles give `{"error": f"No data for {tf}"}`;
   - otherwise use `build_tf_entry`, and append `(tf, bias_num)` to the scores.
4. If **no** timeframe succeeded, re-raise the first `ScreenerServiceError`. If none was raised (all were empty), raise `NO_DATA`.
5. Return:

```python
{"symbol": "HYPERLIQUID:BTC", "coin": "BTC", "exchange": "HYPERLIQUID", "source": "hyperliquid",
 "analysis_type": "Multi-Timeframe Alignment",
 "timeframes": {tf: entry_or_error for tf in MTF_TIMEFRAMES},     # insertion order = MTF_TIMEFRAMES
 "alignment": alignment, "recommendation": recommendation,
 "not_available": list(NOT_AVAILABLE)}
```

That is one `metaAndAssetCtxs` request (cached) plus five `candleSnapshot` requests, with intervals `1w`, `1d`, `4h`, `1h` and `15m`.

### 2.4 Backtest candle-source switch (WP-BT)

**2.4.1 `src/core/services/backtest_service.py`.** These are the only edits:

| Line | Change |
|---|---|
| 22 | `from typing import Optional` becomes `from typing import Callable, Optional` |
| 612–613 (`run_backtest`), 690–691 (`compare_strategies`), 779–780 (`walk_forward_backtest`) | After `periods_per_year: Optional[int] = None,` insert `*,` and `fetch_ohlcv: Optional[Callable[[str, str, str], list]] = None,` |
| 630, 705, 824 | `candles = _fetch_ohlcv(symbol, period, interval)` becomes `candles = (fetch_ohlcv or _fetch_ohlcv)(symbol, period, interval)` |

`_fetch_ohlcv` must still be looked up through the module global at call time. Existing tests and the routing tripwire monkeypatch `backtest_service._fetch_ohlcv`. Nothing else changes: validation order, legacy `{"error": str}` messages and result keys all stay the same.

**2.4.2 `src/core/services/smart_money_service.py`.** These are the only edits:

| Line | Change |
|---|---|
| 25 | add `Callable` to `from typing import Any, Dict, List, Optional` |
| 278–279 (`analyze_smart_money` signature) | after `interval: str = "1d",` insert `*,` and `fetch_ohlcv: Optional[Callable[[str, str, str], List[dict]]] = None,` |
| 293 | `candles = _fetch_ohlcv(ysym, period, interval)` becomes `candles = (fetch_ohlcv or _fetch_ohlcv)(ysym, period, interval)` |

Why this module needs its own parameter: it binds `_fetch_ohlcv` at import (`from ...backtest_service import _fetch_ohlcv`, line 28), so a switch inside `backtest_service` alone would not reach it.

**2.4.3 `src/core/services/hyperliquid_backtest.py` (new).**

```python
HL_PERIODS_PER_YEAR: dict[str, int] = {"1d": 365, "1h": 8760}    # 24/7 market; see D6
DATA_SOURCE: str = "Hyperliquid"

class HyperliquidCandleFetcher:
    """One per tool call. Passed as fetch_ohlcv=... to backtest/smart-money services."""
    meta: dict | None        # fetch_hyperliquid_ohlcv() result, after a successful call
    error: Exception | None  # the exception raised by the fetch, if any
    def __init__(self) -> None
    def __call__(self, symbol: str, period: str, interval: str) -> list[dict]
        # result = hl_svc.fetch_hyperliquid_ohlcv(symbol, period, interval)
        # On ANY Exception: store it in self.error, then re-raise it.
        # On success: self.meta = result; return result["candles"].

def decorate_result(result: dict, fetcher: HyperliquidCandleFetcher, *,
                    context: str, periods_per_year: int | None = None) -> dict

def hyperliquid_run_backtest(symbol: str, strategy: str, period: str = "1y",
        initial_capital: float = 10_000.0, commission_pct: float = 0.1, slippage_pct: float = 0.05,
        interval: str = "1d", include_trade_log: bool = False,
        include_equity_curve: bool = False) -> dict
def hyperliquid_compare_strategies(symbol: str, period: str = "1y",
        initial_capital: float = 10_000.0, interval: str = "1d") -> dict
def hyperliquid_walk_forward_backtest(symbol: str, strategy: str, period: str = "2y",
        initial_capital: float = 10_000.0, commission_pct: float = 0.1, slippage_pct: float = 0.05,
        n_splits: int = 3, train_ratio: float = 0.7, interval: str = "1d") -> dict
def hyperliquid_smart_money(symbol: str, period: str = "6mo", interval: str = "1d") -> dict
```

Each wrapper:

- builds a fresh fetcher;
- computes `ppy = HL_PERIODS_PER_YEAR.get((interval or "").strip().lower())`;
- calls the service **through the module attribute** (`backtest_service.run_backtest(...)`, `backtest_service.compare_strategies(...)`, `backtest_service.walk_forward_backtest(...)` or `smart_money_service.analyze_smart_money(symbol, "HYPERLIQUID", period, interval, fetch_ohlcv=f)`), passing `periods_per_year=ppy, fetch_ohlcv=f` by keyword (smart money doesn't take `periods_per_year`);
- returns `decorate_result(...)`.

The whole body sits inside `try/except Exception` and returns `exception_to_envelope(e, context=<tool name>)`, so the wrappers **never raise**. The `context` values are `"backtest_strategy"`, `"compare_strategies"`, `"walk_forward_backtest_strategy"` and `"smart_money_analysis"`.

**`decorate_result`** handles three cases, in order:

1. If `fetcher.error is not None`, return `exception_to_envelope(fetcher.error, context=context)`. This gives a typed envelope (`SYMBOL_NOT_FOUND`, `NO_DATA`, `UPSTREAM_*` or `INVALID_PARAMETER`) instead of the service's string error.
2. Otherwise, if `"error" in result`, return `result` unchanged. These are upstream validation errors: legacy `{"error": str}` from backtests and typed envelopes from smart money. See D8.
3. Otherwise, set or override these keys on `result` and return it:

```python
"symbol": f"HYPERLIQUID:{meta['coin']}", "coin": meta["coin"],
"exchange": "HYPERLIQUID", "source": "hyperliquid", "data_source": DATA_SOURCE,
"requested_bars": meta["requested_bars"], "returned_bars": meta["returned_bars"],
"truncated": meta["truncated"], "data_warning": meta["warning"],     # None unless truncated
# only when periods_per_year is not None:
"periods_per_year": periods_per_year,
```

For the smart-money result, also `pop("yahoo_symbol", None)`.

`compare_strategies` keeps its own `"warnings"` (the SMA200 note), unchanged. The truncation text appears only in `data_warning`.

### 2.5 `src/server.py`. Edits are serialized: WP-R (edit 1), then WP-DOC (edit 2).

**Edit 1 (WP-R).** Re-read the file first. Current line numbers:

| Where | Change |
|---|---|
| Imports, after line 93 (`register_hyperliquid_tools`) | Add three import blocks: `hyperliquid_top_movers` and `hyperliquid_top_movers_async` from `core.services.hyperliquid_movers`; `analyze_hyperliquid_coin` and `analyze_hyperliquid_mtf` from `core.services.hyperliquid_analysis`; and `hyperliquid_run_backtest`, `hyperliquid_compare_strategies`, `hyperliquid_walk_forward_backtest` and `hyperliquid_smart_money` from `core.services.hyperliquid_backtest`. |
| Validators import (lines 94–103) | Add `is_native_provider` and `native_symbol_prefix`. Remove `reject_native_exchange`, which is no longer used. |
| `instructions=` (line 130) | Replace the last sentence with the text below. |
| `top_gainers` (lines 152–163) | `validate_exchange(exchange, "KUCOIN", allow_native=True)`. After `validate_timeframe`, add `if is_native_provider(exchange): return await hyperliquid_top_movers_async(timeframe, losers=False, limit=limit)`. The rest is unchanged. |
| `top_losers` (lines 173–182) | `allow_native=True`. After `validate_timeframe`, add `if is_native_provider(exchange): return hyperliquid_top_movers(timeframe, losers=True, limit=limit)`. |
| `coin_analysis` (lines 260–265) | `allow_native=True`. After `validate_timeframe`, add `if is_native_provider(exchange) or native_symbol_prefix(symbol): return analyze_hyperliquid_coin(symbol, timeframe)`. |
| `smart_money_analysis` (line 639) | Replace `reject_native_exchange(exchange)` with `if is_native_provider(exchange) or native_symbol_prefix(symbol): return hyperliquid_smart_money(symbol, period, interval)`. |
| `multi_timeframe_analysis` (lines 689–691) | `allow_native=True`. Directly after it, before `normalize_tradingview_symbol`, add `if is_native_provider(exchange) or native_symbol_prefix(symbol): return await analyze_hyperliquid_mtf(symbol)`. |
| `combined_analysis` (line 744) | The first statement inside the existing `try:` becomes `reject_native_symbol_prefix(symbol)`. The tool does **not** opt in: `validate_exchange` keeps its default `allow_native=False`. |
| `backtest_strategy` (`return run_backtest(` at line 827) | Insert before that return: `if native_symbol_prefix(symbol): return hyperliquid_run_backtest(symbol, strategy, period, initial_capital, commission_pct, slippage_pct, interval, include_trade_log, include_equity_curve)` |
| `compare_strategies` (`return` at line 851) | Insert before that return: `if native_symbol_prefix(symbol): return hyperliquid_compare_strategies(symbol, period, initial_capital, interval)` |
| `walk_forward_backtest_strategy` (`return walk_forward_backtest(` at line 882) | Insert before that return: `if native_symbol_prefix(symbol): return hyperliquid_walk_forward_backtest(symbol, strategy, period, initial_capital, commission_pct, slippage_pct, n_splits, train_ratio, interval)` |
| `yahoo_price` (line 897) | Before the `return`, add `try: reject_native_symbol_prefix(symbol)` / `except Exception as e: return exception_to_envelope(e, context="yahoo_price")`. |

Routing rules this edit establishes:

- An explicit `HYPERLIQUID:` symbol prefix wins over the `exchange` argument in `coin_analysis`, `multi_timeframe_analysis` and `smart_money_analysis`. This matches the existing convention that `normalize_tradingview_symbol` lets an explicit prefix override `exchange` (D2).
- Backtests route **only** on the prefix. A bare `BTC` still goes to Yahoo.
- Every branch sits inside the handler's existing `try`. The backtest branches need no `try`, because the wrappers never raise.

New `instructions` last sentence (exact):

```
Hyperliquid perpetuals (native API, not TradingView): use exchange="HYPERLIQUID" with top_gainers/top_losers (timeframe="1D" only), coin_analysis, multi_timeframe_analysis and smart_money_analysis; pass symbol="HYPERLIQUID:<COIN>" to backtest_strategy, compare_strategies and walk_forward_backtest_strategy; hyperliquid_market_overview, hyperliquid_funding_rates and hyperliquid_orderbook are Hyperliquid-only. Every other tool rejects HYPERLIQUID with UNSUPPORTED_EXCHANGE.
```

`exchanges_list()` (line 1193) needs no edit. It renders `native_support_hint`, which now lists all 11 tools.

**Edit 2 (WP-DOC): docstrings only, no code.** Required content for each docstring (wording is free):

| Tool | Must mention |
|---|---|
| `top_gainers`, `top_losers` | `HYPERLIQUID` in the exchange list; with Hyperliquid, `timeframe` must be `"1D"` (24h change from mark price vs previous-day price), and other timeframes give `INVALID_TIMEFRAME`; rows add `volume_24h_usd`, `open_interest_usd` and `funding_rate_1h`. The strings `HYPERLIQUID`, `1D` and `24h` must appear. |
| `coin_analysis`, `multi_timeframe_analysis` | `HYPERLIQUID` in the exchange list; the accepted symbol forms (`BTC`, `BTCUSDT`, `BTC-PERP`, `HYPERLIQUID:BTC`, `kPEPE`); computed from Hyperliquid candles with `source: "hyperliquid"` and no TradingView rating (`Recommend.All`); see `not_available`. |
| `smart_money_analysis` | `exchange="HYPERLIQUID"` or a `HYPERLIQUID:<COIN>` symbol uses Hyperliquid candles instead of Yahoo. |
| `backtest_strategy`, `compare_strategies`, `walk_forward_backtest_strategy` | `symbol="HYPERLIQUID:<COIN>"` (e.g. `HYPERLIQUID:BTC`) uses Hyperliquid perp candles; Hyperliquid serves only the most recent 5,000 candles, so `1h` beyond about 208 days (`1y`, `2y`) is truncated and flagged in `data_warning`; annualization uses 365 days / 8,760 hours. The strings `HYPERLIQUID:` and `5,000` must appear. |

Don't touch `bollinger_scan`, `combined_analysis` or any other docstring.

### 2.6 `src/hyperliquid_tools.py` (WP-DOC, docstrings only)

In all three tool docstrings, replace `Other tools reject exchange="HYPERLIQUID".` with:

```
Hyperliquid also works in top_gainers/top_losers (timeframe "1D"), coin_analysis, multi_timeframe_analysis, smart_money_analysis and the backtest tools (symbol "HYPERLIQUID:<COIN>"); every other tool rejects exchange="HYPERLIQUID".
```

No code changes.

---

## 3. Work packages and file ownership

| WP | Files (exclusive) | Model | Depends on | When |
|---|---|---|---|---|
| **R**: routing and server wiring | `src/core/utils/validators.py`; **`src/server.py` edit 1** | **opus** | M, TA and BT modules must exist (server imports them at import time) | validators.py in Wave 1; server.py slot 1 in Wave 2, after M, TA and BT land |
| **M**: movers | `src/core/services/hyperliquid_movers.py` | **sonnet** | none (uses step-1 provider API) | Wave 1 |
| **TA**: analysis adapter | `src/core/services/hyperliquid_analysis.py` | **sonnet** | none | Wave 1 |
| **BT**: backtest/smart-money switch | `src/core/services/hyperliquid_backtest.py`, `src/core/services/backtest_service.py` (section 2.4.1 only), `src/core/services/smart_money_service.py` (section 2.4.2 only) | **sonnet** | none | Wave 1 |
| **DOC**: docstrings | `src/hyperliquid_tools.py` (docstrings); **`src/server.py` edit 2** | **haiku** | R's server edit | hyperliquid_tools.py in Wave 1; server.py slot 2 in Wave 3 (last) |
| **T**: tester | `tests/fixtures/hyperliquid/capture_fixtures_step2.py` and its outputs; `tests/unit/hyperliquid/conftest.py`; new `tests/unit/hyperliquid/test_hl_movers.py`, `test_hl_analysis.py`, `test_hl_backtest.py`, `test_hl_opt_in.py`; updates to `test_hl_routing_safety.py`, `test_hl_tools.py`, `test_hl_dev_a.py`, `test_hl_dev_d.py`, `test_hl_coinlist_and_docs.py`, `tests/unit/test_strict_validation.py`, `tests/unit/test_exchange_aliases.py`; `tests/stress/test_hl_live.py` | **sonnet** | the spec only | Wave 1 (fixtures and tests); runs the suite after Wave 3 |

**Ownership rules:**

- **`server.py` is sequential: R, then DOC.** Each re-reads the file before editing.
- Developers may add their own tests **only** in `tests/unit/hyperliquid/test_hl_dev2_<wp>.py` (`test_hl_dev2_r.py`, `_m.py`, `_ta.py`, `_bt.py`). The `dev2` infix keeps basenames unique against step 1's `test_hl_dev_<x>.py`, because tests use rootdir import mode with no `__init__.py`.
- Any test that reaches a Hyperliquid code path must live under `tests/unit/hyperliquid/`, where the autouse mock transport applies. Elsewhere it would hit the live network.
- **Interim breakage is expected.** Once WP-R changes `NATIVE_PROVIDER_TOOLS`, the step-1 tests that hard-code the three-tool list fail until WP-T updates them (section 5.2). Run the full suite only after Wave 3.
- Nobody edits `hyperliquid_service.py`, `hyperliquid_symbols.py`, `screener_service.py`, `coinlist.py`, `screener_provider.py`, `errors.py`, `coinlist/*.txt`, `pyproject.toml`, `README.md`, `CHANGELOG.md` or `openclaw/`.

---

## 4. Acceptance criteria

**These apply to every package:**

- `uv run pytest tests/unit -q` gives **673 + new tests, 0 failures**. Existing tests change only as listed in section 5.2.
- `uvx ruff check --isolated --select E9,F63,F7,F82 <changed .py files>` reports nothing.
- No new dependencies, and the tool count stays at 42.

**WP-R:**

- `NATIVE_PROVIDER_TOOLS["hyperliquid"]` equals the 11-tuple in section 2.1, in that order.
- `native_symbol_prefix` behaves as its docstring says.
- Each of the 9 non-opted-in tools with an `exchange` parameter still returns the `UNSUPPORTED_EXCHANGE` envelope with zero network I/O. They are `bollinger_scan`, `rating_filter`, `consecutive_candles_scan`, `advanced_candle_pattern`, `volume_breakout_scanner`, `volume_confirmation_analysis`, `smart_volume_scanner`, `multi_agent_analysis` and `combined_analysis`.
- The 5 opted-in exchange tools and the 3 backtests route as in section 2.5.
- No Hyperliquid call reaches `get_multiple_analysis`, `_scan_with_retry`, `fetch_atr_for_ticker`, `backtest_service._fetch_ohlcv` or `smart_money_service._fetch_ohlcv`.
- `combined_analysis("HYPERLIQUID:BTC")` and `yahoo_price("HYPERLIQUID:BTC")` return `UNSUPPORTED_EXCHANGE` without calling Marketaux, TradingView or Yahoo.
- TradingView, EGX and Yahoo behavior for every other input is unchanged.

**WP-M:**

- Rankings match section 2.2, from one `metaAndAssetCtxs` request.
- A non-`1D` timeframe gives `INVALID_TIMEFRAME` with `valid_timeframes == ["1D"]` and zero requests.
- The sync function uses only the sync client, and the async function only the async client.

**WP-TA:**

- Keys and shapes match section 2.3. Values equal `indicators_calc` applied to the fixture.
- Sub-$1 prices keep 6 significant digits.
- No `Recommend.All` and no `tv_recommendation` key anywhere in the output.
- MTF requests the five intervals once each. A failing timeframe degrades to `{"error": ...}` without failing the tool, and if all five fail, a typed envelope is returned.
- Each path uses only its own client.

**WP-BT:**

- The `fetch_ohlcv=` seam works in all four service functions, and leaving it out keeps today's behavior.
- `HYPERLIQUID:` backtests return `data_source == "Hyperliquid"`, the decorated keys and `periods_per_year`, and report truncation for `1h` with periods over `6mo`.
- Data errors are typed envelopes. Validation errors are returned before any Hyperliquid request.
- The wrappers never raise.

**WP-DOC:**

- The descriptions of the eight tools contain the strings required in section 2.5 (edit 2).
- No `hyperliquid_*` description contains "Other tools reject".
- The `bollinger_scan` and `combined_analysis` descriptions don't mention HYPERLIQUID.

**WP-T:**

- Every test in section 5 exists and passes.
- Fixtures were captured with the script in section 5.0.
- The live file obeys the 30 s ceiling.

---

## 5. Test list (tester)

Write these from this spec, without reading the developers' code. The step-1 testing rules still apply:

- Derive dynamic expectations from the fixtures.
- Synthetic `httpx.Response`s are allowed only for error paths.
- Use the `no_network` fixture on every tool-level test.

**Tripwires.** Reuse `_Tripwires` from `test_hl_routing_safety.py` or copy it. Positive Hyperliquid-path tests arm only the "TV + Yahoo" set:

- `screener_service.get_multiple_analysis`
- `screener_provider._scan_with_retry`
- `screener_provider.fetch_atr_for_ticker`
- `server.fetch_trending_analysis`
- `server.analyze_coin`
- `server.run_multi_timeframe_analysis`
- `backtest_service._fetch_ohlcv`
- `smart_money_service._fetch_ohlcv`
- `server.analyze_smart_money`
- `server.run_backtest`
- `server._compare_strategies`
- `server.walk_forward_backtest`

The wrappers call the services through the service modules, not through these `server.` names.

### 5.0 Fixtures

Add `tests/fixtures/hyperliquid/capture_fixtures_step2.py`. It isn't collected, is run once with `uv run python tests/fixtures/hyperliquid/capture_fixtures_step2.py`, and its outputs are committed. It **must not overwrite** step-1 files.

```python
"""Capture step-2 candle fixtures (live network). Not a test. Leaves step-1 files alone."""
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

def candles(coin, iv, n, ms):
    body = {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": iv, "startTime": now - n * ms, "endTime": now}}
    return post(body)[-n:]

files = {
    "candle_snapshot_BTC_1d.json":  candles("BTC", "1d", 400, 86_400_000),
    "candle_snapshot_BTC_4h.json":  candles("BTC", "4h", 300, 14_400_000),
    "candle_snapshot_BTC_15m.json": candles("BTC", "15m", 300, 900_000),
    "candle_snapshot_BTC_1w.json":  candles("BTC", "1w", 300, 604_800_000),
}
assert len(files["candle_snapshot_BTC_1d.json"]) == 400
assert len(files["candle_snapshot_BTC_4h.json"]) == 300
assert len(files["candle_snapshot_BTC_15m.json"]) == 300
assert len(files["candle_snapshot_BTC_1w.json"]) >= 200      # live: 301 weekly bars (2026-10-04)
for name, data in files.items():
    dump(name, data)
dump("capture_info_step2.json", {"captured_at_ms": now, "url": URL, "files": sorted(files)})
```

**`conftest.py` changes** (additive; step-1 behavior must stay identical):

- **Interval routing in `HLMock.route`.** For `candleSnapshot` on a known coin other than `MATIC`, `RNDR` and `kPEPE`, choose the fixture by `req.interval`: `1d` to BTC_1d, `4h` to BTC_4h, `15m` to BTC_15m, `1w` to BTC_1w, and **any other interval** (including `1h`) to the existing BTC_1h fixture. There's no time-window filtering: the mock returns the whole fixture for any window, so bar counts in tests equal the fixture length.
- **New opt-in fixture `frozen_now`.** It monkeypatches `hyperliquid_service._now_ms` to return `capture_info_step2.json["captured_at_ms"]`, and returns that value. Use it in tests that assert request windows.

### 5.1 Routing safety: `test_hl_routing_safety.py` (update) and `test_hl_opt_in.py` (new)

**Updates to `test_hl_routing_safety.py`:**

1. Set `NATIVE_OPT_IN = {"top_gainers", "top_losers", "coin_analysis", "multi_timeframe_analysis", "smart_money_analysis"}` and add `SYMBOL_PREFIX_OPT_IN = {"backtest_strategy", "compare_strategies", "walk_forward_backtest_strategy"}`. The parametrized test 1 stays unchanged. Its discovery still excludes only `NATIVE_OPT_IN`, so every non-opted-in exchange tool, current or future, stays covered.
2. Change `test_discovery_finds_the_expected_tools` to expect at least 9 tools, including `{"combined_analysis", "multi_agent_analysis", "bollinger_scan", "rating_filter", "volume_confirmation_analysis"}`, and to check that the discovered names are disjoint from `NATIVE_OPT_IN`.
3. New `test_opt_in_tables_match_hint_table`. It checks two things:
   - the set of tools that have an `exchange` property and appear in `NATIVE_PROVIDER_TOOLS["hyperliquid"]` equals `NATIVE_OPT_IN`;
   - `set(NATIVE_PROVIDER_TOOLS["hyperliquid"]) == set(HL_NEW_TOOLS) | NATIVE_OPT_IN | SYMBOL_PREFIX_OPT_IN`, where `HL_NEW_TOOLS` is the three `hyperliquid_*` names.

   This stops a tool from being exempted from the safety test without also being in the hint table.
4. Replace `test_coin_analysis_rejects_hyperliquid_prefix` with two tests:
   - `test_volume_confirmation_rejects_hyperliquid_prefix`: `server.volume_confirmation_analysis("HYPERLIQUID:BTC", exchange="KUCOIN")` returns `UNSUPPORTED_EXCHANGE`, with no network and no tripwire hit;
   - `test_combined_analysis_rejects_hyperliquid_prefix`: `await server.combined_analysis("HYPERLIQUID:BTC", exchange="NASDAQ")` returns a top-level `UNSUPPORTED_EXCHANGE` envelope, and the tripwires `server.analyze_coin`, `server.analyze_sentiment` and `server.fetch_news_summary` are never called.
5. New `test_yahoo_price_rejects_hyperliquid_prefix`: `await server.yahoo_price("hyperliquid:btc")` returns `UNSUPPORTED_EXCHANGE`, and a `server.get_price_async` tripwire is never called.
6. Tests 4–7, 9 and 10 stay unchanged. Test 9 checks that `smart_money_analysis("COMI", "EGX")` still reaches `analyze_smart_money`.

**New `test_hl_opt_in.py`:**

7. **Opted-in tools refuse the paths they can't serve.** Parametrized; each case asserts the error code, `hl_mock.count == 0` (unless noted), `no_network == []` and no TV/Yahoo tripwire hit:
   - `top_gainers` and `top_losers` with `exchange="HYPERLIQUID"` and `timeframe` in `["5m", "15m", "1h", "4h", "1W", "1M"]` give `INVALID_TIMEFRAME`, with `valid_timeframes == ["1D"]`, `exchange == "HYPERLIQUID"` and `retryable is False`. The message contains `1D` and `24h`.
   - The default call `top_gainers(exchange="HYPERLIQUID")` gives `INVALID_TIMEFRAME` (see D1).
   - `coin_analysis("BTC", "HYPERLIQUID", "30m")` gives `INVALID_TIMEFRAME`.
   - `multi_timeframe_analysis("BINANCE:BTCUSDT", "HYPERLIQUID")` gives `INVALID_PARAMETER`. A `metaAndAssetCtxs` request is allowed here, but there must be **no** `candleSnapshot` request.
   - `smart_money_analysis("BTC", "HYPERLIQUID", interval="4h")` and `smart_money_analysis("BTC", "HYPERLIQUID", period="5y")` give `INVALID_PARAMETER`.
   - `backtest_strategy("HYPERLIQUID:BTC", "rsi", interval="4h")`, `backtest_strategy("HYPERLIQUID:BTC", "nope")` and `compare_strategies("HYPERLIQUID:BTC", period="5y")` return a dict with a **string** `"error"` (the legacy backtest format).
8. **Opted-in tools serve Hyperliquid without touching TradingView or Yahoo.** Parametrized over the cases below, each run via `await tool.run(args)` from `server.mcp._tool_manager`, with `no_network` and the TV + Yahoo tripwires armed:
   - `top_gainers {"exchange": "HYPERLIQUID", "timeframe": "1D"}`
   - `top_losers {"exchange": "hyperliquid", "timeframe": "1d"}`
   - `coin_analysis {"symbol": "BTC", "exchange": "HYPERLIQUID", "timeframe": "1h"}`
   - `multi_timeframe_analysis {"symbol": "BTC", "exchange": "HYPERLIQUID"}`
   - `smart_money_analysis {"symbol": "BTC", "exchange": "HYPERLIQUID"}`
   - `backtest_strategy {"symbol": "HYPERLIQUID:BTC", "strategy": "rsi"}`
   - `compare_strategies {"symbol": "HYPERLIQUID:BTC"}`
   - `walk_forward_backtest_strategy {"symbol": "HYPERLIQUID:BTC", "strategy": "rsi"}`

   Each result is not an error envelope. Dict results have `source == "hyperliquid"`; list results have every row's `source == "hyperliquid"`. Every recorded request is one of `metaAndAssetCtxs` or `candleSnapshot`.
9. **Prefix routing** (D2):
   - `server.coin_analysis("HYPERLIQUID:BTC")` with the default exchange KUCOIN, and `server.coin_analysis("hyperliquid:eth", exchange="BINANCE")`, both take the Hyperliquid path: `source == "hyperliquid"`, and the coin is `BTC` / `ETH` respectively.
   - The same holds for `await server.multi_timeframe_analysis("HYPERLIQUID:BTC")` and for `server.smart_money_analysis("HYPERLIQUID:kPEPE")` with the default exchange EGX.
   - TV and Yahoo tripwires are never hit.
10. **Client discipline:**
    - With `hyperliquid_service.get_client` monkeypatched to raise, `top_gainers(HL, "1D")` and `multi_timeframe_analysis("BTC", HL)` still succeed.
    - With `hyperliquid_service.get_async_client` monkeypatched to raise, `top_losers(HL, "1D")`, `coin_analysis("BTC", HL, "1h")`, `smart_money_analysis("BTC", HL)` and `backtest_strategy("HYPERLIQUID:BTC", "rsi")` still succeed.
    - An `async def` test calls `server.coin_analysis("BTC", "HYPERLIQUID", "1h")` directly from inside the running loop, and it succeeds, because no `asyncio.run` is involved.
11. **`native_symbol_prefix`** returns the values in the docstring table of section 2.1.
12. **TradingView paths are unchanged:**
    - `top_gainers(exchange="KUCOIN")` with `server.fetch_trending_analysis` monkeypatched still calls it, and makes zero Hyperliquid requests.
    - `backtest_strategy("BTC", "rsi")` with `backtest_service._fetch_ohlcv` monkeypatched to a recorder (returning 60 synthetic daily candles) calls the recorder with `"BTC"`, returns `data_source == "Yahoo Finance"`, and makes zero Hyperliquid requests. A bare `BTC` is not Hyperliquid.

### 5.2 Required updates to existing tests (exact)

| File | Change |
|---|---|
| `tests/unit/test_strict_validation.py` | Rename `TestToolBoundary.test_top_gainers_rejects_hyperliquid` to `test_top_gainers_hyperliquid_needs_1d`, asserting `INVALID_TIMEFRAME` and `valid_timeframes == ["1D"]`. Rename `test_coin_analysis_rejects_hyperliquid` to `test_coin_analysis_hyperliquid_validates_timeframe`: `server.coin_analysis("BTC", exchange="hyperliquid", timeframe="30m")` gives `INVALID_TIMEFRAME`. Add `test_bollinger_scan_rejects_hyperliquid` (`UNSUPPORTED_EXCHANGE`). All three are network-free. |
| `tests/unit/test_exchange_aliases.py` | `TestNativeProviders.test_native_support_hint` asserts the new exact 11-tool string from section 2.1. |
| `tests/unit/hyperliquid/test_hl_dev_a.py` | `HL_TOOLS` becomes the full 11-name list. `test_native_support_hint_exact` uses the new string. Its other assertions (`supported_tools == HL_TOOLS`) then hold unchanged. |
| `tests/unit/hyperliquid/test_hl_tools.py` | In `test_registered_names_match_hint_table`, replace the set equality with `NATIVE_PROVIDER_TOOLS["hyperliquid"][:3] == tuple(NAMES)`. In `test_server_registers_exactly_the_hyperliquid_tools`, assert `names == set(NAMES)`. |
| `tests/unit/hyperliquid/test_hl_dev_d.py` | In `test_names_match_native_provider_tools_when_available`, assert `set(EXPECTED) <= set(NATIVE_PROVIDER_TOOLS["hyperliquid"])`. |
| `tests/unit/hyperliquid/test_hl_coinlist_and_docs.py` | Add: every name in `NATIVE_PROVIDER_TOOLS["hyperliquid"]` appears in `server.mcp.instructions` and in `server.exchanges_list()`; plus the docstring tests in section 5.6. |
| `tests/unit/hyperliquid/test_hl_routing_safety.py` | As in section 5.1. |

### 5.3 Movers: `test_hl_movers.py`

- `check_movers_timeframe` accepts `"1D"`, `"1d"` and `" 1d "`. It rejects `"15m"`, `"4h"`, `"1W"`, `"1M"` and `""` with the exact extras from section 2.2.
- `build_mover_rows(markets, losers=False, limit=5)` (markets from `parse_markets(fixture)`) is sorted by `change_pct_24h` descending and equals the first 5 of the fixture's live markets sorted that way, with `None` changes excluded. With `losers=True` it is ascending. The row key set is exact. `symbol` keeps canonical case (`HYPERLIQUID:kPEPE` if present). `indicators.open == prev_day_price` and `indicators.close == mark_price`.
- Limit clamping: `limit=0` gives 1 row; `limit=1000` gives `min(50, live_count)` rows.
- No delisted coin (`MATIC`, `RNDR`) ever appears.
- At tool level, `top_gainers(HL, "1D", limit=3)` returns exactly the top 3 from the fixture with exactly 1 recorded request, and `top_losers` mirrors it.
- Persistent 500 on `metaAndAssetCtxs` gives an `UPSTREAM_ERROR` envelope with `retryable is True` for both tools.

### 5.4 Analysis: `test_hl_analysis.py`

- **`indicator_snapshot`:**
  - On the BTC 1h fixture, every key in section 2.3 is present.
  - `RSI == calc_rsi(closes, 14)[-1]`, `EMA20 == calc_ema(closes, 20)[-1]`, `MACD.macd` and `BB.upper` likewise, and `close` is the last close.
  - With 10 synthetic candles, `SMA200`, `EMA200`, `RSI` and `MACD.signal` are `None`, and nothing raises.
  - `[]` raises `NO_DATA`.
- **`coin_analysis("BTC", "HYPERLIQUID", "1h")`:**
  - The top-level key set is exact.
  - `symbol == "HYPERLIQUID:BTC"`, `interval == "1h"`, `bars_used == len(BTC_1h fixture)`.
  - `rsi.value == round(calc_rsi(...)[-1], 2)`.
  - `perp_context` equals the BTC market row.
  - `not_available` contains `"Recommend.All"`.
  - A recursive key scan finds no key `"Recommend.All"` and no key `"tv_recommendation"`.
  - `market_sentiment.overall_rating == compute_bb_rating_signal(...)[0]`.
- **Request** (with `frozen_now`): exactly one `candleSnapshot` with `req.coin == "BTC"`, `req.interval == "1h"`, and `endTime - startTime == 300 * 3_600_000`.
- **Sub-$1 coin.** `coin_analysis("KPEPE", "HYPERLIQUID", "1D")`:
  - `coin == "kPEPE"`, the request coin is `"kPEPE"`, the interval is `"1d"`;
  - `price_data.close == float(f"{last_close:.6g}")`, not `round(x, 4)`;
  - `ema.ema20 == float(f"{calc_ema(closes, 20)[-1]:.6g}")`;
  - `sma.sma200 is None` (60 bars).
- **Errors:**
  - `NOTACOIN` gives `SYMBOL_NOT_FOUND`, and there is no `candleSnapshot` request.
  - `MATIC` gives `SYMBOL_NOT_FOUND` with `delisted is True`.
  - A scripted empty candle list gives `NO_DATA`.
  - Persistent 500 gives `UPSTREAM_ERROR` with `retryable is True`.
- **`build_alignment`** on synthetic scores:
  - all five `+1` gives `"FULLY ALIGNED BULLISH"`;
  - all five `-1` gives `"FULLY ALIGNED BEARISH"`;
  - `[1, 1, 1, 0, 0]` gives `"MOSTLY BULLISH"` with net 3;
  - `[("1W", 1), ("1D", 1), ("4h", 1), ("1h", -1), ("15m", 0)]` gives `"LEAN BULLISH"` with `divergent_timeframes == ["1h"]`;
  - `[]` gives `"MIXED/RANGING"` with confidence `"Low"`;
  - the `recommendation.rules` list has 5 items.
- **`multi_timeframe_analysis("BTC", "HYPERLIQUID")`:**
  - `list(timeframes) == ["1W", "1D", "4h", "1h", "15m"]`, and each entry has the exact key set from section 2.3.
  - The recorded `candleSnapshot` intervals equal `{"1w", "1d", "4h", "1h", "15m"}`, one each, all with coin `BTC`. There's exactly one `metaAndAssetCtxs` request.
  - `alignment.scores_by_tf` keys are a subset of the five timeframes.
  - `source == "hyperliquid"`.
- **Partial failure.** A `candleSnapshot` script callable that returns 500 only for `interval == "4h"`: `timeframes["4h"]` has `"error"`, the other four entries are full, and `"4h"` is not in `scores_by_tf`.
- **Total failure.** Persistent 500 on every `candleSnapshot` gives an `UPSTREAM_ERROR` envelope for the tool.
- **Unknown symbol** gives a `SYMBOL_NOT_FOUND` envelope with no `candleSnapshot` request.

### 5.5 Backtests and smart money: `test_hl_backtest.py`

- **Service seams** (no Hyperliquid involved):
  - `backtest_service.run_backtest("X", "rsi", "1y", fetch_ohlcv=fake)` uses `fake(…)` with `("X", "1y", "1d")`, and `_fetch_ohlcv` (patched to raise) is never called. The same holds for `compare_strategies`, `walk_forward_backtest` and `smart_money_service.analyze_smart_money("X", "EGX", fetch_ohlcv=fake)`, where the fake receives the Yahoo symbol `X.CA`.
  - Without `fetch_ohlcv`, the patched `_fetch_ohlcv` is called. This is today's behavior.
- **`backtest_strategy("HYPERLIQUID:BTC", "rsi", "1y", interval="1d")`** (with `frozen_now`):
  - no error; `symbol == "HYPERLIQUID:BTC"`, `coin == "BTC"`, `exchange == "HYPERLIQUID"`, `source == "hyperliquid"`, `data_source == "Hyperliquid"`;
  - `candles_analyzed == 400`, `truncated is False`, `data_warning is None`, `requested_bars == 365`, `periods_per_year == 365`;
  - the request has `req.interval == "1d"` and `endTime - startTime == 365 * 86_400_000`.
- **Truncation:**
  - `backtest_strategy("HYPERLIQUID:BTC", "rsi", "1y", interval="1h")` gives `truncated is True`, `requested_bars == 8760`, `data_warning` containing `"5,000"`, `periods_per_year == 8760` and `candles_analyzed == 200`.
  - `"6mo"` / `"1h"` gives `truncated is False` (4320).
  - `walk_forward_backtest_strategy("HYPERLIQUID:BTC", "rsi", "2y", interval="1h")` gives `truncated is True`. With 200 1h bars and 3 folds, each test slice is 20 bars, which clears rsi's 19-bar minimum (warmup 14 + 5), so the result is not an error.
- **`compare_strategies("hyperliquid:kpepe", "3mo", interval="1d")`:** `symbol == "HYPERLIQUID:kPEPE"`, the request coin is `"kPEPE"`, `len(ranking) == 9`, `data_source == "Hyperliquid"`, and the `warnings` key is still present (the SMA200 note, since there are only 60 bars).
- **`walk_forward_backtest_strategy("HYPERLIQUID:BTC", "rsi", "2y")`:** no error, `len(folds) >= 1`, `data_source == "Hyperliquid"`.
- **Typed data errors**, checked for each of the three backtest tools:
  - `HYPERLIQUID:NOTACOIN` gives a **dict** envelope with `error.code == "SYMBOL_NOT_FOUND"` (not a string), and there is no `candleSnapshot` request.
  - `HYPERLIQUID:MATIC` gives `delisted is True`.
  - Scripted empty candles give `NO_DATA`.
  - Persistent 500 gives `UPSTREAM_ERROR` with `retryable is True`.
- **Wrappers never raise.** With `hyperliquid_service.fetch_hyperliquid_ohlcv` monkeypatched to raise `ValueError`, each wrapper returns an `INTERNAL_ERROR` (or other typed) envelope.
- **`smart_money_analysis("BTC", "HYPERLIQUID")`** (default `6mo`/`1d`, with `frozen_now`):
  - no error; `source == "hyperliquid"`, `exchange == "HYPERLIQUID"`, `symbol == "HYPERLIQUID:BTC"`, no `yahoo_symbol` key;
  - `bars_analyzed == 400`; the request window is `180 * 86_400_000`;
  - the `mcdx_style`, `banker_fund_oscillator` and `consensus` keys are present.
  - With `interval="1h"`, it gives `truncated is False` and `requested_bars == 4320`.
  - With `NOTACOIN`, it gives `SYMBOL_NOT_FOUND`.

### 5.6 Docs: additions to `test_hl_coinlist_and_docs.py`

- For each of the eight opted-in tools, `"HYPERLIQUID"` is in `tool.description`. `top_gainers` and `top_losers` also contain `"1D"` and `"24h"`. The three backtest tools contain `"HYPERLIQUID:"` and `"5,000"`.
- No `hyperliquid_*` tool description contains `"Other tools reject"`.
- The `bollinger_scan` and `combined_analysis` descriptions don't contain `"HYPERLIQUID"`.
- `server.mcp.instructions` contains `'timeframe="1D"'`, `"HYPERLIQUID:<COIN>"` and every name in `NATIVE_PROVIDER_TOOLS["hyperliquid"]`.

### 5.7 Live: `tests/stress/test_hl_live.py` (update)

**Ceiling, with no new dependency** (D11):

- Add `HL_LIVE_CEILING_S = 30.0`. Every test becomes `async def` and wraps its work in `await asyncio.wait_for(<coro>, timeout=HL_LIVE_CEILING_S)`, the same idiom as upstream's `tests/stress/test_real_upstream.py:65-68`.
- Sync calls go through `asyncio.to_thread(...)` inside `wait_for`.
- The autouse fixture sets `HYPERLIQUID_MCP_TIMEOUT_S=8` and `HYPERLIQUID_MCP_RETRY_DELAYS=0.5,1.0` with `monkeypatch.setenv` **before** `configure_default_clients()`. That bounds one request at about 26 s (3 × 8 s + 1.5 s).
- There's no `conftest.py` in `tests/stress/`: the ceiling must not change upstream's stress tests.

**Tests:**

- Keep the five existing checks, wrapped as above.
- **Reverse drift.** Take the names listed in `hyperliquid.txt` and the live, non-delisted universe. Assert `len(listed & live) / len(listed) >= 0.90`. On failure, the message lists up to 20 stale names (`sorted(listed - live)`) and says to regenerate with `uv run python scripts/gen_hyperliquid_coinlist.py`.
- `await server.top_gainers(exchange="HYPERLIQUID", timeframe="1D", limit=5)` returns a list of 5 rows, all with `source == "hyperliquid"`.
- `server.coin_analysis("BTC", "HYPERLIQUID", "1h")` (via `to_thread`) has `"error"` absent and `bars_used >= 250`.
- `await server.multi_timeframe_analysis("BTC", "HYPERLIQUID")` has at least 4 timeframes without `"error"`.
- `server.backtest_strategy("HYPERLIQUID:BTC", "rsi", "1y", interval="1d")` has `data_source == "Hyperliquid"` and `truncated is False`. With `interval="1h"` it has `truncated is True`, and `returned_bars` is between 4900 and 5002.
- `server.smart_money_analysis("HYPERLIQUID:ETH")` has `"error"` absent.

### 5.8 Smoke checklist (smoke-tester, live, no mocks)

Run each of these and report the result:

- `top_gainers(exchange="HYPERLIQUID", timeframe="1D")`
- `top_losers(exchange="HYPERLIQUID", timeframe="1D")`
- `top_gainers(exchange="HYPERLIQUID")`: expect `INVALID_TIMEFRAME`
- `coin_analysis("kPEPE", "HYPERLIQUID", "4h")`
- `coin_analysis("HYPERLIQUID:BTC")`
- `multi_timeframe_analysis("HYPE", "HYPERLIQUID")`
- `smart_money_analysis("BTC", "HYPERLIQUID", "1y")`
- `backtest_strategy("HYPERLIQUID:BTC", "supertrend", "2y")`
- `backtest_strategy("HYPERLIQUID:BTC", "rsi", "1y", interval="1h")`: expect `data_warning`
- `compare_strategies("HYPERLIQUID:SOL")`
- `walk_forward_backtest_strategy("HYPERLIQUID:ETH", "macd")`
- `combined_analysis("HYPERLIQUID:BTC")`: expect `UNSUPPORTED_EXCHANGE`
- `bollinger_scan(exchange="HYPERLIQUID")`: expect `UNSUPPORTED_EXCHANGE`

---

## 6. Risks and decisions for the user

Items marked **[USER]** needed a decision. **Decided 2026-10-04: D1 strict `INVALID_TIMEFRAME`, D2 the prefix wins and routes to Hyperliquid, D6 365 / 8,760 periods per year, all as specified below.**

- **D1. [USER] Default timeframe on the movers.** `top_gainers` and `top_losers` default to `timeframe="15m"`, so `top_gainers(exchange="HYPERLIQUID")` fails with `INVALID_TIMEFRAME` (hint: pass `"1D"`) unless the caller adds `timeframe="1D"`. This follows the plan ("reject other timeframes"), and the client corrects itself in one round trip.
  - Alternative: silently treat any timeframe as 24h on Hyperliquid. That contradicts the plan.
  - Alternative: change the default to `""`. That changes the TradingView tool schema in an upstream file.
  - **Default: strict.**
- **D2. [USER] An explicit `HYPERLIQUID:` prefix routes natively in `coin_analysis`, `multi_timeframe_analysis` and `smart_money_analysis`,** even when `exchange` is KUCOIN, BINANCE or EGX.
  - Without this, `coin_analysis("HYPERLIQUID:BTC")` with the default exchange would return `UNSUPPORTED_EXCHANGE` whose hint lists `coin_analysis` itself as supported, which contradicts itself.
  - It matches the existing convention that an explicit prefix overrides `exchange` (`normalize_tradingview_symbol`), and the backtests, which have no `exchange` parameter, route on the prefix anyway.
  - It flips step-1 test 5.1.8 for `coin_analysis` (section 5.1 item 4 replaces it).
  - **Default: route.**
- **D3. `smart_money_analysis` opts in (recommended yes).** It costs one keyword parameter and one call-site change in `smart_money_service.py`, plus a two-line server branch that replaces the step-1 guard. Without it, carry-over 2 would still need a guard for `HYPERLIQUID:`-prefixed symbols, which today go to Yahoo with `exchange="EGX"`.
- **D4. `combined_analysis` rejects `HYPERLIQUID:` symbols outright** with `UNSUPPORTED_EXCHANGE`, using one line inside its existing `try`. Today its technical part fails while its sentiment and news parts send the raw symbol to Marketaux and return a normal-looking response. It doesn't opt in: TradingView-backed sentiment isn't meaningful for perps, and the hint points to `coin_analysis`.
- **D5. `yahoo_price` rejects `HYPERLIQUID:` symbols** (UNSUPPORTED_EXCHANGE, with a hint) in 3 lines. The other symbol-only tools (`stock_extended_hours`, the options tools, `market_sentiment`, `financial_news`, `stock_prices`) still pass the raw string to Yahoo or Marketaux and get that upstream's not-found error. None of them reaches TradingView, so the CLAUDE.md rule holds. Guarding them is a cheap follow-up if you want it.
- **D6. [USER] Annualization for Hyperliquid backtests is 365 (1d) and 8,760 (1h)** periods per year, passed through the existing `periods_per_year` parameter. The upstream default of 252 / 1,638 is a US-equity session count and understates Sharpe on 24/7 perps. Yahoo crypto (`BTC-USD`) keeps the upstream default, so Sharpe values aren't directly comparable across the two sources. **Default: 365 / 8,760.** Drop `HL_PERIODS_PER_YEAR` to keep the upstream numbers.
- **D7. `coin_analysis` adds `perp_context`** (mark price, funding, open interest, 24h change) from the already-cached market snapshot, with zero extra requests. This is a small extension beyond the plan's "subset we can compute". Delete the key if unwanted.
- **D8. Backtest validation errors stay in the legacy `{"error": "<string>"}` format** on the Hyperliquid path (unknown strategy, bad period or interval, not enough bars), because they come from upstream `backtest_service`. Only Hyperliquid data errors become typed envelopes. Converting the legacy strings would mean matching on message text or editing upstream validation, which conflicts with the rule to keep upstream edits small.
- **D9. Plan and code disagreements, with the smallest corrections applied:**
  - `smart_money_service` imports `_fetch_ohlcv` by value (line 28), so the plan's single "candle-source switch" can't cover it from `backtest_service`. Both services get the same `fetch_ohlcv=` seam.
  - The plan's "each validates with `allow_native=True`" doesn't apply to the backtests (no `exchange` parameter) or to smart money (never calls `validate_exchange`). They route on the symbol prefix or the `exchange` string.
  - Step 1 left two statements that become false once Step 2 lands: the `instructions` sentence ("available only through" the three tools) and the `hyperliquid_tools.py` docstrings ("Other tools reject"). Both are updated (sections 2.5 and 2.6).
  - Verified live: BTC has 301 weekly bars, so the weekly EMA200 bias works for BTC. Newer coins have less history, and their 1W bias falls back to "Neutral" when EMA200 is `None`, which is the same rule TradingView data follows when a value is missing.
- **D10. Deferred step-1 minor items:**
  - The `hyperliquid_symbols` empty-after-strip fix. That file isn't touched, and no live coin is named or suffixed `PERP`/`USD`/`USDE`.
  - Diff-shrinking helpers for `exchanges_list()` and `symbol_not_found_error`. `screener_service.py` isn't touched, and they would only move about 3 lines in `server.py`.
- **D11. Live-test ceiling.** This uses `asyncio.wait_for` (upstream's own idiom in `test_real_upstream.py`) plus environment-bounded httpx timeouts, scoped to `test_hl_live.py` only.
  - SIGALRM was rejected: Unix-only, main-thread-only, and it would need a conftest that also affects upstream stress tests.
  - `pytest-timeout` was rejected: it's a new dependency.
  - Limitation: `wait_for` around `to_thread` fails the test at 30 s but can't kill the worker thread. The thread stops within the about 26 s httpx bound.
- **D12. In-progress bar.** Analysis includes the still-forming last candle, as TradingView's real-time indicators and Yahoo's backtest data do. If you'd rather use closed bars only, drop the last candle in `indicator_snapshot`.

## 7. As-built notes (2026-10-04, after review)

- **Docstrings are additions only.** Section 2.5 edit 2 asked for `HYPERLIQUID` in the `exchange:` Args lines of the mover and analysis tools. To avoid rebase conflicts, every upstream docstring line is kept verbatim, and Hyperliquid support is described only in the added paragraphs. The section 5.6 tests pass against the paragraphs.
- **Annualization wording** in the backtest docstrings is scoped to `HYPERLIQUID:` symbols (365 / 8,760). Yahoo symbols keep 252 / 1,638.
- **Movers timeframe check.** For `exchange="HYPERLIQUID"`, the native branch runs before `validate_timeframe`, so any non-1D timeframe (e.g. `30m`) returns the movers `INVALID_TIMEFRAME` with `valid_timeframes=["1D"]`. The TradingView path is unchanged.
- **`instructions` wording** is narrowed to "Every other tool that takes an exchange rejects HYPERLIQUID". Symbol-only D5 tools (`stock_extended_hours`, the options tools, `market_sentiment`, `financial_news`) still pass the raw symbol to Yahoo or Marketaux, never TradingView. `test_symbol_only_tools_guard_hyperliquid_prefix` enforces this.
- **Live cap.** `candleSnapshot` can return slightly over 5,000 bars (5,003 seen live), so the live bound is 5,010.
