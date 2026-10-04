"""hyperliquid.txt coinlist and Phase 7 metadata (spec step-1 section 5.6)."""
from __future__ import annotations

import os
import re

from tradingview_mcp import server
from tradingview_mcp.core.services.coinlist import load_symbols
from tradingview_mcp.core.utils.validators import COINLIST_DIR

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
