"""WP-C developer tests: pure Hyperliquid symbol resolution."""

import pytest

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services.hyperliquid_symbols import (
    build_name_index,
    candidate_coin_names,
    resolve_coin,
    to_coinlist_line,
)

K_COINS = ["kPEPE", "kSHIB", "kBONK", "kFLOKI", "kLUNC", "kDOGS", "kNEIRO"]
LIVE = build_name_index(["BTC", "ETH", "SOL", "PEPE2", *K_COINS])
DELISTED = build_name_index(["MATIC"])


@pytest.mark.parametrize(
    "raw",
    ["BTC", "btc", " BTC ", "BTCUSDT", "BTCUSDC", "BTCUSD", "BTC-PERP", "BTC.P",
     "BTCUSDT.P", "BTC-USD", "BTC/USDC", "BTC_USDT", "HYPERLIQUID:BTC",
     "hyperliquid:btcusdc"],
)
def test_btc_variants(raw):
    assert resolve_coin(raw, LIVE) == "BTC"


@pytest.mark.parametrize(
    "raw",
    ["KPEPE", "kpepe", "kPEPE", "KPEPEUSDT", "HYPERLIQUID:KPEPE", "1000PEPE", "1000PEPEUSDT"],
)
def test_kpepe_variants(raw):
    assert resolve_coin(raw, LIVE) == "kPEPE"


@pytest.mark.parametrize("coin", K_COINS)
def test_k_coins_from_uppercase(coin):
    assert resolve_coin(coin.upper(), LIVE) == coin


def test_exact_match_beats_suffix_stripping():
    idx = build_name_index(["S", "SUSD"])
    assert resolve_coin("SUSD", idx) == "SUSD"
    assert resolve_coin("SUSDT", idx) == "S"


def test_foreign_prefix():
    with pytest.raises(ScreenerServiceError) as e:
        candidate_coin_names("BINANCE:BTCUSDT")
    assert e.value.code == ErrorCode.INVALID_PARAMETER


@pytest.mark.parametrize("raw", ["", "HYPERLIQUID:", "-PERP"])
def test_empty_input(raw):
    with pytest.raises(ScreenerServiceError) as e:
        resolve_coin(raw, LIVE)
    assert e.value.code == ErrorCode.INVALID_PARAMETER


def test_unknown_coin():
    with pytest.raises(ScreenerServiceError) as e:
        resolve_coin("NOTACOIN", LIVE, DELISTED)
    assert e.value.code == ErrorCode.SYMBOL_NOT_FOUND
    assert e.value.extra["retryable"] is False
    assert e.value.extra["exchange"] == "HYPERLIQUID"
    assert isinstance(e.value.extra["suggestions"], list)
    assert len(e.value.extra["suggestions"]) <= 5


def test_delisted_coin():
    with pytest.raises(ScreenerServiceError) as e:
        resolve_coin("MATIC", LIVE, DELISTED)
    assert e.value.code == ErrorCode.SYMBOL_NOT_FOUND
    assert e.value.extra["delisted"] is True


def test_candidate_order_and_dedupe():
    assert candidate_coin_names("1000pepe") == ["1000PEPE", "KPEPE"]
    assert candidate_coin_names("BTC") == ["BTC"]


def test_index_collision():
    with pytest.raises(ValueError):
        build_name_index(["kPEPE", "KPEPE"])


def test_coinlist_line():
    assert to_coinlist_line("kPEPE") == "HYPERLIQUID:kPEPE"
