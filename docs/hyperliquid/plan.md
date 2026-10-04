# Plan: Add Hyperliquid as a native (non-TradingView) venue

_Drafted 2026-10-03 · Revised 2026-10-04 after a feasibility review against the codebase and live APIs_

## Key finding

**TradingView does not carry Hyperliquid.** Verified live:

- The TradingView crypto scanner returns 0 rows for `exchange == "HYPERLIQUID"` (re-checked 2026-10-04).
- `TA_Handler(exchange="HYPERLIQUID")` returns "symbol not found" for `BTCUSDC.P`, `BTCUSD.P`, `HYPEUSDC.P` and `HYPEUSDC`.
- Hyperliquid is not among the 100+ crypto exchanges the scanner lists.

So Hyperliquid can't be added like Bybit or OKX (one line in `EXCHANGE_SCREENER`). It needs its own data provider.

Hyperliquid's public info API (`POST https://api.hyperliquid.xyz/info`) works without a key. `metaAndAssetCtxs` and `candleSnapshot` both returned live data, which fits the project's "no API keys" approach. As of 2026-10-04 there are 234 perps in the universe, 56 of them marked `isDelisted`. Each asset context carries `markPx`, `prevDayPx`, `dayNtlVlm`, `funding` and `openInterest`.

> If you only want to analyze the **HYPE token**, that already works today through CEX listings such as `KUCOIN:HYPEUSDT`. This plan is for Hyperliquid as a venue: its perps, funding and open interest.

## Scope (v1)

Read-only **perpetuals** data. No trading, which matches the README's "does not execute trades" stance. Spot markets and builder-deployed (HIP-3) perps are deferred.

**For my fork only, with no pull requests.** This repo is my fork (`origin` = `antonium85/tradingview-mcp-local`). Commit to a branch such as `local/hyperliquid` and push it to `origin` freely. Don't open a pull request, either within the fork or to the upstream project. Run the server from that branch.

Because the branch will have to be rebased whenever upstream changes are pulled in, keep the diff to existing files small:

- Put the new logic in new files (`hyperliquid_service.py`, the new tools, new test files).
- In existing files, limit changes to the hooks: the `allow_native` validation, the fallback filter, and the per-tool branches.

## Phase 1: Data provider (`core/services/hyperliquid_service.py`)

- **Both a sync and an async client**, the way `yahoo_finance_service.py` does it (sync `get_price` plus async `get_price_async`):
  - Most handlers that need Hyperliquid data are plain `def`s: `coin_analysis`, `top_losers`, `smart_money_analysis`, `backtest_strategy`, `compare_strategies` and `walk_forward_backtest_strategy`. FastMCP 1.x runs sync tools directly on the event loop, so they can't await an async client, and `asyncio.run()` inside them would fail because a loop is already running. These use a sync `httpx.Client`.
  - `top_gainers`, `multi_timeframe_analysis` and the new Phase 5 tools are `async def` and use `httpx.AsyncClient`.
  - Both share one request builder, response parser and TTL cache, so only the transport differs.
- Retries with backoff, and a short TTL cache like `screener_provider.py` has (`TRADINGVIEW_MCP_CACHE_TTL`).
- Endpoints to use:
  - `metaAndAssetCtxs`: the market list plus mark price, `prevDayPx`, 24h volume, funding and open interest, all in one call.
  - `candleSnapshot`: OHLCV data.
  - `fundingHistory` (requires `startTime`) and `l2Book`.
- Map timeframes: `5m/15m/1h/4h/1D/1W/1M` → `5m/15m/1h/4h/1d/1w/1M`.
- Constraints to handle:
  - The API returns only the **most recent 5,000 candles**. At 1h that's about 208 days. Verified: a 400-day 1h request returned 5,002 bars.
  - Rate limits are weight-based per IP, so throttle requests. The existing TA throttle is a `threading.Semaphore` (`screener_provider.py:269`), which works for the sync client but would block the event loop if the async path used it. The async client gets its own `asyncio.Semaphore`, and both limiters draw on one shared weight budget.
  - Skip markets marked `isDelisted`.
- Raise `ScreenerServiceError` with the existing error codes so the tool wrappers produce the usual typed error envelopes.

## Phase 2: Symbol normalization

- Users will type `BTCUSDT`, `BTCUSDC`, `BTC-PERP`, `BTC.P` or `HYPERLIQUID:BTC`. All of these should resolve to the coin name `BTC`.
- **Coin names are case-sensitive, but our symbol path uppercases them.** `validators.py:172` and `:178` call `.upper()` before handlers see the symbol, so `kPEPE` arrives as `KPEPE`. Normalization must:
  1. strip the prefix and the quote/perp suffix,
  2. look the result up **case-insensitively** in the live market list (a `{name.upper(): name}` map built from `metaAndAssetCtxs`), and
  3. send the **canonical** name (`kPEPE`) to the API.

  Coins with the `k` prefix (1,000× units) found live: `kPEPE`, `kSHIB`, `kBONK`, `kLUNC`, `kFLOKI`, `kDOGS`.
- Add `coinlist/hyperliquid.txt` (lines as `HYPERLIQUID:<COIN>`, matching the other lists), generated from the market list. See Phase 3 for keeping it out of the fallback and suggestion paths that would misroute it.

## Phase 3: Routing in `core/utils/validators.py`

- Add a separate `NATIVE_PROVIDERS = {"hyperliquid"}` set, kept apart from `EXCHANGE_SCREENER`. This follows the same reasoning as `_TA_ONLY_SCREENERS`: anything in `EXCHANGE_SCREENER` gets sent to the TradingView scanner, which would return nothing for Hyperliquid.
- **Don't widen `validate_exchange` / `sanitize_exchange` for every caller.** `server.py` has 13 call sites, and `get_market_type()` defaults unknown exchanges to `"crypto"`. If Hyperliquid were accepted everywhere, any tool that forgot its explicit reject would quietly send `HYPERLIQUID` to TradingView and return an empty result, not an error. Instead, make native support opt-in:
  - Add an `allow_native: bool = False` parameter to `validate_exchange` / `sanitize_exchange`, or a separate `validate_native_exchange()`.
  - Only the tools in Phase 4 pass `allow_native=True` and branch on it.
  - Everywhere else, `hyperliquid` fails validation by default with the error described below. That makes "secure by default" the behavior, rather than something each handler has to remember.
- **Error code:** `ErrorCode` has no `UNSUPPORTED_EXCHANGE` today, only `INVALID_EXCHANGE`. Add `UNSUPPORTED_EXCHANGE = "UNSUPPORTED_EXCHANGE"` to `core/errors.py`. It's distinct from `INVALID_EXCHANGE`: the venue exists, but this tool can't serve it. Its hint names the tools that do support Hyperliquid, so the AI can correct itself. Add it to the error-code tests.
- **Keep Hyperliquid out of the TradingView fallback, which takes more than leaving it out of `_FALLBACK_VENUE_PREFERENCE`.** `pick_fallback_exchange` (`screener_service.py:550`) returns `candidates[0]` when no preferred venue matches, and the candidates come from `exchanges_listing_symbol()`, which indexes every coinlist file. A bare `BTC` is listed only in `hyperliquid.txt`, so `BINANCE:BTC` would "fall back" to `HYPERLIQUID` and get routed to the TradingView scanner. Filter `NATIVE_PROVIDERS` out of the candidates in `pick_fallback_exchange`.
- `exchanges_listing_symbol()` may still return `HYPERLIQUID` for **error hints**. When it does, the hint should say it's only supported by the Phase 4/5 tools, so the AI doesn't retry it on an unsupported tool.

## Phase 4: Existing tools that should support Hyperliquid

Each of these validates with `allow_native=True` and gets a `hyperliquid` branch at the top of its handler. The sync/async column says which client the handler uses (see Phase 1).

| Tool | Handler | Approach |
|---|---|---|
| `top_gainers` / `top_losers` | async / **sync** | Rank from `metaAndAssetCtxs` (`markPx` vs `prevDayPx`). 24h change only, so reject other timeframes with `INVALID_TIMEFRAME` and a clear hint. |
| `coin_analysis`, `multi_timeframe_analysis` | **sync** / async | Compute from candles with `indicators_calc.py` (`calc_rsi`, `calc_bollinger`, `calc_macd`, `calc_ema`, `calc_atr`, `calc_supertrend`). Return the subset we can compute and set `source: "hyperliquid"`; don't invent TradingView's `Recommend.All` rating. |
| `backtest_strategy`, `compare_strategies`, `walk_forward_backtest_strategy` | **sync** | See "Backtests" below. |
| `smart_money_analysis` | **sync** | Optional. It already runs on candles (`_fetch_ohlcv` via `yahoo_symbol_for`), so it needs the same candle-source switch as the backtests. |

### Backtests

The backtest tools take **Yahoo symbols** (`BTC-USD`, `AAPL`) and **Yahoo `period` ranges** (`1mo`, `3mo`, `6mo`, `1y`, `2y`), which go straight into Yahoo's `range=` parameter in `_fetch_ohlcv` (`backtest_service.py:62`). The Hyperliquid branch therefore needs to:

- Detect symbols prefixed `HYPERLIQUID:` and route them to a sync `fetch_hyperliquid_ohlcv(coin, period, interval)` instead of Yahoo.
- Convert `period` into a `startTime`/`endTime` window (`1mo` → 30 days, … `2y` → 730 days).
- Return candles in the same shape `_fetch_ohlcv` does (`date` formatted the same way for `1d` and `1h`, OHLCV rounded to 4 dp), so the strategy engine and `smart_money_service` don't change.
- Add a **truncation warning** whenever the requested window is longer than 5,000 bars. For `1h` that's any period over ~208 days, **including the `1y` default**, so expect it often. `1d` backtests fit easily (`2y` = 730 bars).

Every other tool that takes an exchange (Bollinger scan, rating filter, candle patterns, volume scanners, `combined_analysis`, and so on) doesn't opt in, so it returns the `UNSUPPORTED_EXCHANGE` envelope from Phase 3 with no per-handler code.

## Phase 5: New tools specific to Hyperliquid

This is perp data TradingView doesn't offer, so these tools are the main reason to integrate natively:

- `hyperliquid_market_overview`: ranks markets by volume (`dayNtlVlm`), open interest and funding.
- `hyperliquid_funding_rates`: most positive and most negative funding now, plus history for one coin (`fundingHistory`).
- `hyperliquid_orderbook` (optional): top-of-book depth and spread (`l2Book`).

Make these `async def` and use the async client. They need the same `ToolAnnotations` as the other tools (read-only, open-world) so `test_tool_annotations.py` keeps passing.

## Phase 6: Tests

- Unit tests with mocked HTTP:
  - symbol normalization, including uppercased input (`KPEPE` → `kPEPE`) and an unknown coin
  - timeframe mapping and period → time-window conversion
  - gainer/loser ranking, skipping delisted markets
  - the 5,000-bar truncation warning
  - error envelopes, including the new `UNSUPPORTED_EXCHANGE`
  - the sync and async clients returning identical parsed results
- **Routing guards** (Hyperliquid must never reach the TradingView scanner):
  - Every tool that doesn't opt in rejects `hyperliquid` with `UNSUPPORTED_EXCHANGE`. Parametrize the test over all tools that take an `exchange` argument, so a newly added tool is covered automatically.
  - `pick_fallback_exchange("BTC", "BINANCE")` never returns `HYPERLIQUID`.
- Extend `test_exchange_aliases.py` and `test_strict_validation.py`.
- Add a live test to `tests/stress/test_real_upstream.py`.

## Phase 7: Docs and metadata

- The `exchanges://list` resource (`server.py:1180`) builds its list from the files in `coinlist/`, so adding `hyperliquid.txt` advertises Hyperliquid there automatically, which would suggest it works for every tool. Either exclude `NATIVE_PROVIDERS` from that list and add a separate line ("HYPERLIQUID: perps only, supported by top_gainers, top_losers, coin_analysis, …"), or label the entry inline. Also update the hard-coded fallback string at `server.py:1198`.
- Tool docstrings (`server.py:136`, `:190`) and the hard-coded crypto list in `combined_analysis` (`server.py:739`). That tool doesn't opt in, but it shouldn't be left with an inconsistent list.
- **Leave the public-facing docs alone:** the README (tool count and feature table), `CHANGELOG.md` and `openclaw/SKILL.md`. Nobody else reads them for this build, and every edit there would conflict when upstream changes are pulled in. The docstrings and resources above do matter, because the AI client reads them at runtime.

## Open questions

1. **Perps only, or spot too?** Spot uses awkward `@index` / `PURR/USDC` naming. Suggest deferring it.
2. **Support in existing tools (Phase 4), new tools only (Phase 5), or both?** Phase 5 alone is the smallest change; Phase 4 is what makes "top gainers on Hyperliquid" work.
3. **Build order (local commits, no PRs):**
   - **Step 1:** Phases 1–3 plus the new tools. The opt-in validation, the `UNSUPPORTED_EXCHANGE` code and the fallback filter must go in the same commit as `hyperliquid.txt`, because adding that file changes `pick_fallback_exchange` behavior immediately.
   - **Step 2:** routing the existing tools and backtests.

## Build

The build is done by subagents defined in `.claude/agents/`. Shared rules are in `CLAUDE.md`. The main session coordinates: it launches each role, sends test failures back to the right developer, runs the suite between waves, and is the only one that commits and pushes to `local/hyperliquid`.

The baseline before starting was `uv run pytest tests/unit -q` → **286 passed** (2026-10-04).

### Roles

| Agent | Model | Does |
|---|---|---|
| `architect` | Opus | Writes `docs/hyperliquid/step-N-spec.md`: the interfaces, which files each developer owns, the acceptance criteria and the test list. Doesn't write code. |
| `developer` | Sonnet (default) | Implements one work package in the files it owns, plus that package's unit tests. The model is chosen per package: **Opus** for the data provider (sync/async clients, shared cache and rate limits) and the routing safety checks; **Sonnet** for symbols, new tools, routing the existing tools and the backtest adapter; **Haiku** for docstrings, `exchanges://list` and generating `coinlist/hyperliquid.txt`. |
| `tester` | Sonnet | Writes tests from the spec without reading the developers' code, mocking the API with saved real responses in `tests/fixtures/hyperliquid/`. Writes the parametrized routing-safety test. Runs the suite and reports failures. |
| `reviewer` | Opus | Read-only review of the step's diff against the spec, the plan and the silent-bug rules in `CLAUDE.md`. |
| `smoke-tester` | Haiku | Calls the new and changed tools against the real Hyperliquid API, without mocks. |
| `upstream-sync` | Sonnet | Rebases `local/hyperliquid` onto upstream `main` later on. Never pushes. |

### Flow for each step

1. **`architect`** writes `docs/hyperliquid/step-N-spec.md`. I check it, and any risks it flags go to the user for a decision.
2. **`developer` × N** work in parallel on separate files, while the **`tester`** writes tests from the spec. Edits to `server.py` happen one developer at a time.
3. **`tester`** runs `uv run pytest tests/unit -q`. Failures go back to the developer who owns the file, at most 2 rounds; after that, the main session steps in.
4. **`smoke-tester`** runs the tools against the live API.
5. **`reviewer`** checks the diff. Its findings get fixed and the tests rerun.
6. The **main session** commits and pushes to `origin/local/hyperliquid`. No PR.

### Steps

- **Step 1** (`step-1-spec.md`): Phases 1–3 plus the new Phase 5 tools, and the docs/metadata parts of Phase 7 that apply.
- **Step 2** (`step-2-spec.md`): Phase 4, which routes the existing tools and the backtests.
