# tradingview-mcp-local

A personal fork of `atilaahmettaner/tradingview-mcp`: a Python MCP server (FastMCP 1.x) for TradingView screening, TA and backtests. The source is in `src/tradingview_mcp/`.

## Commands

- Unit tests (hermetic, about 6 s): `uv run pytest tests/unit -q`. The baseline on 2026-10-04 was 286 passed.
- Live-network tests: `uv run pytest -m stress tests/stress`. Run these only when asked; they hit real upstream APIs.
- Lint for real bugs: `uvx ruff check --isolated --select E9,F63,F7,F82 <files>`. ruff is not a project dependency, so don't add it.
  - A PostToolUse hook runs this check on every edited `.py` file.
  - Don't fix the ~470 pre-existing style warnings under ruff's default rules.

## Git and repo rules

- This is a fork. Push branches to `origin` freely, but **never open a pull request**, whether to the fork or upstream. `gh pr create` is denied in `.claude/settings.json`.
- Feature work goes on `local/<feature>` branches. Only the main session commits and pushes; subagents leave changes uncommitted.
- Keep changes to existing upstream files small, because every edit there is a future rebase conflict. Put new logic in new files.
- Don't edit `README.md`, `CHANGELOG.md` or `openclaw/`. Those are upstream's public docs.
- Plans and specs live in `docs/<feature>/`. The current feature is `docs/hyperliquid/` (`plan.md`, `step-N-spec.md`).

## Code rules that cause silent bugs

- **Sync tools can't await.** FastMCP 1.x runs a plain `def` tool on the event loop. Never call an async client or `asyncio.run()` from one; give it a sync client instead.
- **Venues TradingView doesn't carry** (`NATIVE_PROVIDERS`, e.g. Hyperliquid) must never reach the TradingView scanner or `pick_fallback_exchange`. Tools accept them only with `allow_native=True`.
- **Symbols are uppercased** by `core/utils/validators.py`. Anything case-sensitive, such as Hyperliquid's `kPEPE`, needs a case-insensitive lookup that maps back to the canonical name.
- **Return errors as typed envelopes.** Raise `ScreenerServiceError` / use `ErrorCode` from `core/errors.py`. Never return bare strings or let exceptions escape a tool.
- **Every new tool needs `ToolAnnotations`** (read-only, open-world), or `tests/unit/test_tool_annotations.py` fails.
- **Leave dependency pins alone**, especially `tradingview-screener==3.0.0` and `mcp<2`. See the comments in `pyproject.toml`.

## Subagents

Role definitions are in `.claude/agents/`. The whole `.claude/` folder (agents, settings, hooks) is **local only**: upstream's `.gitignore` excludes it, and that's intended. Don't un-ignore it or try to commit it.

- `architect`: Opus, writes specs
- `developer`: Sonnet by default
- `tester`: Sonnet
- `reviewer`: Opus, read-only
- `smoke-tester`: Haiku
- `upstream-sync`: Sonnet

The build workflow is described in `docs/hyperliquid/plan.md` under "Build".
