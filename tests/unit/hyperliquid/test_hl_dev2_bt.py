"""WP-BT: Hyperliquid candle source for backtest / smart-money services."""
from __future__ import annotations

import pytest

from tradingview_mcp.core.errors import ErrorCode
from tradingview_mcp.core.services import backtest_service, smart_money_service
from tradingview_mcp.core.services import hyperliquid_backtest as hb


def _fake_candles(n=400):
    out = []
    px = 100.0
    for i in range(n):
        px *= 1.01 if (i // 20) % 2 == 0 else 0.99
        out.append({"date": f"d{i}", "open": px, "high": px * 1.01, "low": px * 0.99,
                    "close": px, "volume": 1000.0 + i})
    return out


def _meta(**kw):
    m = {"coin": "BTC", "interval": "1d", "period": "1y", "candles": _fake_candles(),
         "requested_bars": 400, "returned_bars": 400, "truncated": False,
         "warning": None, "source": "hyperliquid"}
    m.update(kw)
    return m


@pytest.fixture
def fake_fetch(monkeypatch):
    calls = []

    def _f(symbol, period, interval="1d"):
        calls.append((symbol, period, interval))
        return _meta(interval=interval)

    monkeypatch.setattr(hb.hl_svc, "fetch_hyperliquid_ohlcv", _f)
    return calls


def test_constants():
    assert hb.HL_PERIODS_PER_YEAR == {"1d": 365, "1h": 8760}
    assert hb.DATA_SOURCE == "Hyperliquid"


def test_fetcher_success_and_error(monkeypatch):
    f = hb.HyperliquidCandleFetcher()
    assert f.meta is None and f.error is None
    monkeypatch.setattr(hb.hl_svc, "fetch_hyperliquid_ohlcv", lambda *a: _meta())
    assert len(f("BTC", "1y", "1d")) == 400 and f.meta["coin"] == "BTC"

    def boom(*a):
        raise ValueError("x")
    monkeypatch.setattr(hb.hl_svc, "fetch_hyperliquid_ohlcv", boom)
    g = hb.HyperliquidCandleFetcher()
    with pytest.raises(ValueError):
        g("BTC", "1y", "1d")
    assert isinstance(g.error, ValueError)


def test_run_backtest_decorated_365(fake_fetch):
    r = hb.hyperliquid_run_backtest("HYPERLIQUID:BTC", "ema_cross", "1y")
    assert "error" not in r, r
    assert r["symbol"] == "HYPERLIQUID:BTC" and r["coin"] == "BTC"
    assert r["data_source"] == "Hyperliquid" and r["source"] == "hyperliquid"
    assert r["exchange"] == "HYPERLIQUID"
    assert r["periods_per_year"] == 365 and r["sharpe_periods_per_year"] == 365
    assert r["truncated"] is False and r["data_warning"] is None
    assert fake_fetch[0][2] == "1d"


def test_hourly_uses_8760(fake_fetch):
    r = hb.hyperliquid_run_backtest("BTC", "ema_cross", "1y", interval="1h")
    assert r["sharpe_periods_per_year"] == 8760 and r["periods_per_year"] == 8760


def test_truncation_surfaced(monkeypatch):
    monkeypatch.setattr(hb.hl_svc, "fetch_hyperliquid_ohlcv",
                        lambda *a: _meta(truncated=True, warning="capped at 5000 bars",
                                         requested_bars=9000, returned_bars=5000))
    r = hb.hyperliquid_run_backtest("BTC", "ema_cross", "1y")
    assert r["truncated"] is True and r["data_warning"] == "capped at 5000 bars"
    assert r["requested_bars"] == 9000


def test_compare_and_walk_forward(fake_fetch, monkeypatch):
    c = hb.hyperliquid_compare_strategies("BTC", "1y")
    assert "error" not in c, c
    assert c["coin"] == "BTC" and c["periods_per_year"] == 365
    monkeypatch.setattr(hb.hl_svc, "fetch_hyperliquid_ohlcv",
                        lambda *a: _meta(candles=_fake_candles(730)))
    w = hb.hyperliquid_walk_forward_backtest("BTC", "ema_cross", "2y")
    assert "error" not in w, w
    assert w["data_source"] == "Hyperliquid" and w["periods_per_year"] == 365


def test_smart_money(fake_fetch):
    r = hb.hyperliquid_smart_money("BTC", "6mo")
    assert "error" not in r, r
    assert r["coin"] == "BTC" and "yahoo_symbol" not in r
    assert r["data_source"] == "Hyperliquid" and "periods_per_year" not in r


def test_fetch_error_becomes_typed_envelope(monkeypatch):
    # Real client against the autouse mock: unknown coin -> typed envelope, not a string.
    r = hb.hyperliquid_run_backtest("NOSUCHCOIN", "ema_cross", "1y")
    assert r["error"] is True or isinstance(r.get("error"), dict) or r.get("code")
    assert not isinstance(r.get("error"), str)
    r2 = hb.hyperliquid_smart_money("NOSUCHCOIN", "6mo")
    assert not isinstance(r2.get("error"), str)


def test_validation_error_passthrough(fake_fetch):
    r = hb.hyperliquid_run_backtest("BTC", "nope", "1y")
    assert isinstance(r["error"], str) and "Unknown strategy" in r["error"]
    assert "coin" not in r


def test_service_default_still_uses_module_fetch(monkeypatch):
    monkeypatch.setattr(backtest_service, "_fetch_ohlcv", lambda *a: _fake_candles())
    r = backtest_service.run_backtest("AAPL", "ema_cross", "1y")
    assert r["data_source"] == "Yahoo Finance" and r["sharpe_periods_per_year"] == 252


def test_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(backtest_service, "run_backtest", boom)
    r = hb.hyperliquid_run_backtest("BTC", "ema_cross")
    assert isinstance(r, dict) and not isinstance(r.get("error"), str)


def test_smart_money_few_bars_drops_yahoo_symbol(monkeypatch):
    monkeypatch.setattr(hb.hl_svc, "fetch_hyperliquid_ohlcv",
                        lambda *a: _meta(candles=_fake_candles(20), returned_bars=20))
    r = hb.hyperliquid_smart_money("HYPERLIQUID:BTC", "6mo")
    assert r["error"]["code"] == "NO_DATA"
    assert "yahoo_symbol" not in r["error"]
