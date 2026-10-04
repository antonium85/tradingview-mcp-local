"""Hyperliquid backtests and smart money (spec step-2 sections 2.4 and 5.5)."""
from __future__ import annotations

import importlib
import json
import pathlib

import httpx
import pytest

from tradingview_mcp.core.services import backtest_service, smart_money_service
from tradingview_mcp.core.services import hyperliquid_service as svc

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"
DAY = 86_400_000


def hb():
    return importlib.import_module("tradingview_mcp.core.services.hyperliquid_backtest")


def fx(name):
    return json.loads((FIXTURES / name).read_text())


def bt_candles_1d():
    return svc.to_backtest_candles(svc.parse_candles(fx("candle_snapshot_BTC_1d.json")), "1d")


@pytest.fixture(autouse=True)
def _no_net(no_network):
    yield
    assert no_network == []


@pytest.fixture
def tripwires(monkeypatch):
    hits: list[str] = []

    def make(name):
        def _boom(*a, **k):
            hits.append(name)
            raise AssertionError(f"tripwire hit: {name}")
        return _boom

    monkeypatch.setattr(backtest_service, "_fetch_ohlcv", make("backtest_service._fetch_ohlcv"))
    monkeypatch.setattr(smart_money_service, "_fetch_ohlcv", make("smart_money_service._fetch_ohlcv"))
    return hits


def is_typed(res, code):
    return isinstance(res, dict) and isinstance(res.get("error"), dict) and res["error"]["code"] == code


# ── service seams ────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, symbol, period, interval):
        self.calls.append((symbol, period, interval))
        return bt_candles_1d()


def test_seam_run_backtest(tripwires):
    fake = Recorder()
    res = backtest_service.run_backtest("X", "rsi", "1y", fetch_ohlcv=fake)
    assert "error" not in res, res
    assert fake.calls == [("X", "1y", "1d")]
    assert tripwires == []


def test_seam_compare_strategies(tripwires):
    fake = Recorder()
    res = backtest_service.compare_strategies("X", "1y", fetch_ohlcv=fake)
    assert "error" not in res, res
    assert fake.calls == [("X", "1y", "1d")]
    assert tripwires == []


def test_seam_walk_forward(tripwires):
    fake = Recorder()
    res = backtest_service.walk_forward_backtest("X", "rsi", "2y", fetch_ohlcv=fake)
    assert "error" not in res, res
    assert fake.calls == [("X", "2y", "1d")]
    assert tripwires == []


def test_seam_smart_money(tripwires):
    fake = Recorder()
    res = smart_money_service.analyze_smart_money("X", "EGX", fetch_ohlcv=fake)
    assert "error" not in res, res
    assert fake.calls == [("X.CA", "6mo", "1d")]
    assert tripwires == []


def test_default_still_uses_module_fetch(monkeypatch):
    fake = Recorder()
    monkeypatch.setattr(backtest_service, "_fetch_ohlcv", fake)
    res = backtest_service.run_backtest("X", "rsi", "1y")
    assert "error" not in res, res
    assert fake.calls == [("X", "1y", "1d")]


# ── backtest_strategy ────────────────────────────────────────────────────────

def test_backtest_1d(hl_mock, frozen_now, tripwires):
    res = hb().hyperliquid_run_backtest("HYPERLIQUID:BTC", "rsi", "1y", interval="1d")
    assert "error" not in res, res
    assert res["symbol"] == "HYPERLIQUID:BTC" and res["coin"] == "BTC"
    assert res["exchange"] == "HYPERLIQUID" and res["source"] == "hyperliquid"
    assert res["data_source"] == "Hyperliquid"
    assert res["candles_analyzed"] == 400
    assert res["truncated"] is False and res["data_warning"] is None
    assert res["requested_bars"] == 365
    assert res["periods_per_year"] == 365
    reqs = hl_mock.of_type("candleSnapshot")
    assert len(reqs) == 1
    r = reqs[0]["req"]
    assert r["coin"] == "BTC" and r["interval"] == "1d"
    assert r["endTime"] - r["startTime"] == 365 * DAY
    assert tripwires == []


def test_backtest_1h_truncated(hl_mock, frozen_now):
    res = hb().hyperliquid_run_backtest("HYPERLIQUID:BTC", "rsi", "1y", interval="1h")
    assert "error" not in res, res
    assert res["truncated"] is True
    assert res["requested_bars"] == 8760
    assert "5,000" in res["data_warning"]
    assert res["periods_per_year"] == 8760
    assert res["candles_analyzed"] == 200


def test_backtest_1h_6mo_not_truncated(hl_mock, frozen_now):
    res = hb().hyperliquid_run_backtest("HYPERLIQUID:BTC", "rsi", "6mo", interval="1h")
    assert "error" not in res, res
    assert res["truncated"] is False
    assert res["requested_bars"] == 4320


def test_walk_forward_1h_truncated(hl_mock, frozen_now):
    res = hb().hyperliquid_walk_forward_backtest("HYPERLIQUID:BTC", "rsi", "2y", interval="1h")
    assert "error" not in res, res
    assert res["truncated"] is True
    assert res["periods_per_year"] == 8760


def test_walk_forward_default(hl_mock, frozen_now):
    res = hb().hyperliquid_walk_forward_backtest("HYPERLIQUID:BTC", "rsi")
    assert "error" not in res, res
    assert len(res["folds"]) >= 1
    assert res["data_source"] == "Hyperliquid"
    assert res["symbol"] == "HYPERLIQUID:BTC" and res["source"] == "hyperliquid"


def test_compare_kpepe(hl_mock, frozen_now):
    res = hb().hyperliquid_compare_strategies("hyperliquid:kpepe", "3mo", interval="1d")
    assert "error" not in res, res
    assert res["symbol"] == "HYPERLIQUID:kPEPE" and res["coin"] == "kPEPE"
    assert hl_mock.of_type("candleSnapshot")[0]["req"]["coin"] == "kPEPE"
    assert len(res["ranking"]) == 9
    assert res["data_source"] == "Hyperliquid"
    assert "warnings" in res


# ── Sharpe annualization actually reaches _calc_metrics ──────────────────────

@pytest.fixture
def metrics_spy(monkeypatch):
    seen = {"ppy": [], "ann": []}
    orig = backtest_service._calc_metrics

    def spy(*a, **k):
        out = orig(*a, **k)
        seen["ppy"].append(k.get("periods_per_year"))
        if isinstance(out, dict) and out.get("sharpe_periods_per_year") is not None:
            seen["ann"].append(out["sharpe_periods_per_year"])
        return out

    monkeypatch.setattr(backtest_service, "_calc_metrics", spy)
    return seen


@pytest.mark.parametrize("interval,want", [("1d", 365), ("1h", 8760)])
@pytest.mark.parametrize("which", ["backtest", "compare", "walk_forward"])
def test_annualization_reaches_sharpe(which, interval, want, hl_mock, frozen_now, metrics_spy):
    if which == "backtest":
        res = hb().hyperliquid_run_backtest("HYPERLIQUID:BTC", "rsi", "6mo", interval=interval)
    elif which == "compare":
        res = hb().hyperliquid_compare_strategies("HYPERLIQUID:BTC", "6mo", interval=interval)
    else:
        res = hb().hyperliquid_walk_forward_backtest("HYPERLIQUID:BTC", "rsi", "6mo", interval=interval)
    assert "error" not in res, res
    assert metrics_spy["ppy"] and set(metrics_spy["ppy"]) == {want}
    assert metrics_spy["ann"] and set(metrics_spy["ann"]) == {want}


@pytest.mark.parametrize("which", ["compare", "walk_forward"])
def test_yahoo_path_keeps_252(which, monkeypatch, metrics_spy):
    monkeypatch.setattr(backtest_service, "_fetch_ohlcv", lambda s, p, i: bt_candles_1d())
    if which == "compare":
        res = backtest_service.compare_strategies("X", "1y")
    else:
        res = backtest_service.walk_forward_backtest("X", "rsi", "2y")
    assert "error" not in res, res
    assert set(metrics_spy["ppy"]) == {None}
    assert set(metrics_spy["ann"]) == {252}


# ── typed data errors ────────────────────────────────────────────────────────

CALLS = {
    "backtest": lambda sym: hb().hyperliquid_run_backtest(sym, "rsi", "1y"),
    "compare": lambda sym: hb().hyperliquid_compare_strategies(sym, "1y"),
    "walk_forward": lambda sym: hb().hyperliquid_walk_forward_backtest(sym, "rsi", "2y"),
}


@pytest.mark.parametrize("which", sorted(CALLS))
def test_unknown_coin_typed(which, hl_mock):
    res = CALLS[which]("HYPERLIQUID:NOTACOIN")
    assert is_typed(res, "SYMBOL_NOT_FOUND"), res
    assert hl_mock.of_type("candleSnapshot") == []


@pytest.mark.parametrize("which", sorted(CALLS))
def test_delisted_typed(which, hl_mock):
    res = CALLS[which]("HYPERLIQUID:MATIC")
    assert is_typed(res, "SYMBOL_NOT_FOUND"), res
    assert res["error"]["delisted"] is True


@pytest.mark.parametrize("which", sorted(CALLS))
def test_empty_candles_typed(which, hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(200, json=[])])
    res = CALLS[which]("HYPERLIQUID:BTC")
    assert is_typed(res, "NO_DATA"), res


@pytest.mark.parametrize("which", sorted(CALLS))
def test_persistent_500_typed(which, hl_mock):
    hl_mock.script("candleSnapshot", [httpx.Response(500, text="boom")])
    res = CALLS[which]("HYPERLIQUID:BTC")
    assert is_typed(res, "UPSTREAM_ERROR"), res
    assert res["error"]["retryable"] is True


# ── wrappers never raise ─────────────────────────────────────────────────────

@pytest.mark.parametrize("which", sorted(CALLS))
def test_wrappers_never_raise(which, monkeypatch):
    def boom(*a, **k):
        raise ValueError("kaboom")
    monkeypatch.setattr(svc, "fetch_hyperliquid_ohlcv", boom)
    res = CALLS[which]("HYPERLIQUID:BTC")
    assert isinstance(res, dict) and isinstance(res["error"], dict)


def test_smart_money_wrapper_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ValueError("kaboom")
    monkeypatch.setattr(svc, "fetch_hyperliquid_ohlcv", boom)
    res = hb().hyperliquid_smart_money("BTC")
    assert isinstance(res, dict) and isinstance(res["error"], dict)


# ── smart money ──────────────────────────────────────────────────────────────

def test_smart_money_btc(hl_mock, frozen_now, tripwires):
    res = hb().hyperliquid_smart_money("BTC")
    assert "error" not in res, res
    assert res["source"] == "hyperliquid" and res["exchange"] == "HYPERLIQUID"
    assert res["symbol"] == "HYPERLIQUID:BTC"
    assert "yahoo_symbol" not in res
    assert res["bars_analyzed"] == 400
    r = hl_mock.of_type("candleSnapshot")[0]["req"]
    assert r["endTime"] - r["startTime"] == 180 * DAY
    for k in ("mcdx_style", "banker_fund_oscillator", "consensus"):
        assert k in res
    assert tripwires == []


def test_smart_money_1h(hl_mock, frozen_now):
    res = hb().hyperliquid_smart_money("BTC", interval="1h")
    assert "error" not in res, res
    assert res["truncated"] is False
    assert res["requested_bars"] == 4320


def test_smart_money_unknown(hl_mock):
    res = hb().hyperliquid_smart_money("NOTACOIN")
    assert is_typed(res, "SYMBOL_NOT_FOUND"), res


# ── fetcher / decorate ───────────────────────────────────────────────────────

def test_fetcher_records_meta_and_error(hl_mock):
    f = hb().HyperliquidCandleFetcher()
    assert f.meta is None and f.error is None
    candles = f("BTC", "1y", "1d")
    assert candles and f.meta["coin"] == "BTC" and f.error is None
    g = hb().HyperliquidCandleFetcher()
    with pytest.raises(Exception):
        g("NOTACOIN", "1y", "1d")
    assert g.error is not None


def test_constants():
    assert hb().HL_PERIODS_PER_YEAR == {"1d": 365, "1h": 8760}
    assert hb().DATA_SOURCE == "Hyperliquid"
