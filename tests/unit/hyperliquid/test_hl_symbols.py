"""Symbol normalization for Hyperliquid (pure, spec step-1 section 5.3)."""
from __future__ import annotations

import pytest

from tradingview_mcp.core.errors import ScreenerServiceError
from tradingview_mcp.core.services.hyperliquid_symbols import (
    build_name_index,
    candidate_coin_names,
    resolve_coin,
    to_coinlist_line,
)

import json
import pathlib

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="module")
def indexes():
    universe = load_fixture("meta_and_asset_ctxs.json")[0]["universe"]
    live = build_name_index(u["name"] for u in universe if not u.get("isDelisted"))
    delisted = build_name_index(u["name"] for u in universe if u.get("isDelisted"))
    return live, delisted


def code_of(exc_info) -> str:
    return exc_info.value.to_envelope()["error"]["code"]


@pytest.mark.parametrize("raw", [
    "BTC", "btc", " BTC ", "BTCUSDT", "BTCUSDC", "BTCUSD", "BTC-PERP", "BTC.P",
    "BTCUSDT.P", "BTC-USD", "BTC/USDC", "BTC_USDT", "HYPERLIQUID:BTC", "hyperliquid:btcusdc",
])
def test_resolves_to_btc(raw, indexes):
    live, delisted = indexes
    assert resolve_coin(raw, live, delisted) == "BTC"


@pytest.mark.parametrize("raw", [
    "KPEPE", "kpepe", "kPEPE", "KPEPEUSDT", "HYPERLIQUID:KPEPE", "1000PEPE", "1000PEPEUSDT",
])
def test_resolves_to_kpepe(raw, indexes):
    live, delisted = indexes
    assert resolve_coin(raw, live, delisted) == "kPEPE"


def test_every_k_coin_resolves_from_uppercase(indexes):
    live, delisted = indexes
    ks = [n for n in live.values() if n.startswith("k")]
    assert {"kPEPE", "kBONK", "kSHIB"} <= set(ks)
    for name in ks:
        assert resolve_coin(name.upper(), live, delisted) == name


def test_exact_match_beats_suffix_stripping():
    idx = build_name_index(["S", "SUSD"])
    assert resolve_coin("SUSD", idx) == "SUSD"
    assert resolve_coin("SUSDT", idx) == "S"


def test_candidates_are_uppercase_deduped_most_literal_first():
    c = candidate_coin_names("btcusdt")
    assert c[0] == "BTCUSDT"
    assert "BTC" in c
    assert len(c) == len(set(c))
    assert all(x == x.upper() for x in c)


def test_foreign_prefix_rejected(indexes):
    live, delisted = indexes
    with pytest.raises(ScreenerServiceError) as ei:
        resolve_coin("BINANCE:BTCUSDT", live, delisted)
    assert code_of(ei) == "INVALID_PARAMETER"
    with pytest.raises(ScreenerServiceError) as ei:
        candidate_coin_names("BINANCE:BTCUSDT")
    assert code_of(ei) == "INVALID_PARAMETER"


@pytest.mark.parametrize("raw", ["", "HYPERLIQUID:", "-PERP"])
def test_empty_input_rejected(raw, indexes):
    live, delisted = indexes
    with pytest.raises(ScreenerServiceError) as ei:
        resolve_coin(raw, live, delisted)
    assert code_of(ei) == "INVALID_PARAMETER"


def test_unknown_coin(indexes):
    live, delisted = indexes
    with pytest.raises(ScreenerServiceError) as ei:
        resolve_coin("NOTACOIN", live, delisted)
    err = ei.value.to_envelope()["error"]
    assert err["code"] == "SYMBOL_NOT_FOUND"
    assert err["retryable"] is False
    assert err["exchange"] == "HYPERLIQUID"
    assert isinstance(err["suggestions"], list) and len(err["suggestions"]) <= 5


@pytest.mark.parametrize("raw", ["MATIC", "matic", "RNDR"])
def test_delisted_coin(raw, indexes):
    live, delisted = indexes
    with pytest.raises(ScreenerServiceError) as ei:
        resolve_coin(raw, live, delisted)
    err = ei.value.to_envelope()["error"]
    assert err["code"] == "SYMBOL_NOT_FOUND"
    assert err["delisted"] is True
    assert err["exchange"] == "HYPERLIQUID"


def test_build_name_index_maps_upper_to_canonical():
    assert build_name_index(["kPEPE", "BTC"]) == {"KPEPE": "kPEPE", "BTC": "BTC"}


def test_build_name_index_case_collision_raises():
    with pytest.raises(ValueError):
        build_name_index(["kPEPE", "KPEPE"])


def test_to_coinlist_line():
    assert to_coinlist_line("kPEPE") == "HYPERLIQUID:kPEPE"
    assert to_coinlist_line("BTC") == "HYPERLIQUID:BTC"
