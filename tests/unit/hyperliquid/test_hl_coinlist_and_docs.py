"""hyperliquid.txt coinlist and Phase 7 metadata (spec step-1 section 5.6)."""
from __future__ import annotations

import os
import re

from tradingview_mcp import server
from tradingview_mcp.core.services.coinlist import load_symbols
from tradingview_mcp.core.utils.validators import COINLIST_DIR, NATIVE_PROVIDER_TOOLS

HL_TOOLS = ["hyperliquid_market_overview", "hyperliquid_funding_rates", "hyperliquid_orderbook"]
LINE = re.compile(r"^HYPERLIQUID:[A-Za-z0-9]+$")


def _lines():
    with open(os.path.join(COINLIST_DIR, "hyperliquid.txt"), encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


def test_coinlist_file_format():
    assert "hyperliquid.txt" in os.listdir(COINLIST_DIR)
    lines = _lines()
    assert len(lines) >= 100
    bad = [ln for ln in lines if not LINE.match(ln)]
    assert bad == []


def test_coinlist_content():
    lines = _lines()
    for want in ("HYPERLIQUID:BTC", "HYPERLIQUID:ETH", "HYPERLIQUID:HYPE", "HYPERLIQUID:kPEPE"):
        assert want in lines
    uppers = [ln.upper() for ln in lines]
    assert len(uppers) == len(set(uppers))
    assert "HYPERLIQUID:MATIC" not in uppers and "HYPERLIQUID:RNDR" not in uppers


def test_all_txt_untouched_and_loader_works():
    with open(os.path.join(COINLIST_DIR, "all.txt"), encoding="utf-8") as f:
        assert not [ln for ln in f if ln.upper().startswith("HYPERLIQUID:")]
    assert load_symbols("hyperliquid")


def test_exchanges_list_resource():
    text = server.exchanges_list()
    first = text.splitlines()[0]
    assert first.startswith("Available exchanges:")
    assert "HYPERLIQUID" not in first
    assert "KUCOIN" in first and "BINANCE" in first
    assert "HYPERLIQUID" in text
    for name in HL_TOOLS:
        assert name in text
    assert "UNSUPPORTED_EXCHANGE" in text


def test_exchanges_list_fallback(monkeypatch):
    monkeypatch.setattr(server.os.path, "exists", lambda p: False)
    text = server.exchanges_list()
    assert text.startswith("Common exchanges:")
    first = text.splitlines()[0]
    assert "HYPERLIQUID" not in first
    assert "HYPERLIQUID" in text
    for name in HL_TOOLS:
        assert name in text
    assert "UNSUPPORTED_EXCHANGE" in text


def test_instructions_mention_new_tools():
    for name in HL_TOOLS:
        assert name in server.mcp.instructions


# ── step 2 additions ─────────────────────────────────────────────────────────

OPTED_IN = [
    "top_gainers", "top_losers", "coin_analysis", "multi_timeframe_analysis",
    "smart_money_analysis", "backtest_strategy", "compare_strategies",
    "walk_forward_backtest_strategy",
]


def _descriptions() -> dict[str, str]:
    return {t.name: (t.description or "") for t in server.mcp._tool_manager.list_tools()}


def test_every_native_tool_in_instructions_and_exchanges_list():
    text = server.exchanges_list()
    for name in NATIVE_PROVIDER_TOOLS["hyperliquid"]:
        assert name in server.mcp.instructions, name
        assert name in text, name


def test_instructions_step2_content():
    ins = server.mcp.instructions
    assert 'timeframe="1D"' in ins
    assert "HYPERLIQUID:<COIN>" in ins
    for name in NATIVE_PROVIDER_TOOLS["hyperliquid"]:
        assert name in ins


def test_opted_in_tool_descriptions_mention_hyperliquid():
    d = _descriptions()
    for name in OPTED_IN:
        assert "HYPERLIQUID" in d[name], name


def test_movers_descriptions_mention_1d_and_24h():
    d = _descriptions()
    for name in ("top_gainers", "top_losers"):
        assert "1D" in d[name] and "24h" in d[name], name


def test_backtest_descriptions_mention_prefix_and_truncation():
    d = _descriptions()
    for name in ("backtest_strategy", "compare_strategies", "walk_forward_backtest_strategy"):
        assert "HYPERLIQUID:" in d[name], name
        assert "5,000" in d[name], name


def test_hyperliquid_tool_descriptions_drop_other_tools_reject():
    for name, desc in _descriptions().items():
        if name.startswith("hyperliquid_"):
            assert "Other tools reject" not in desc, name


def test_non_opted_in_descriptions_do_not_mention_hyperliquid():
    d = _descriptions()
    for name in ("bollinger_scan", "combined_analysis"):
        assert "HYPERLIQUID" not in d[name], name
