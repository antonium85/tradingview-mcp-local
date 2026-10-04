"""Hyperliquid technical analysis adapter (coin_analysis / multi_timeframe_analysis).

Indicators are computed from Hyperliquid candles with this server's own
``indicators_calc``; TradingView ratings (Recommend.All) do not exist for this
venue and are never invented. The still-forming last candle is included, like
TradingView's real-time values.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError
from tradingview_mcp.core.services import hyperliquid_service as hl_svc
from tradingview_mcp.core.services import hyperliquid_symbols
from tradingview_mcp.core.services import indicators as _ind
from tradingview_mcp.core.services import indicators_calc as _calc

TA_BARS: int = 300
MTF_TIMEFRAMES: tuple[str, ...] = ("1W", "1D", "4h", "1h", "15m")
MTF_LABELS: dict[str, str] = {
    "1W": "Weekly (Trend Bias)", "1D": "Daily (Swing Setup)", "4h": "4-Hour (Refinement)",
    "1h": "1-Hour (Entry Timing)", "15m": "15-Min (Execution)"}
NOT_AVAILABLE: tuple[str, ...] = (
    "Recommend.All", "Recommend.MA", "Recommend.Other", "ADX", "Stochastic", "VWAP",
    "Pivot support/resistance", "CCI", "Williams %R", "Ichimoku", "Parabolic SAR")
NOTE: str = ("Computed from Hyperliquid candles with this server's own indicator code; "
             "TradingView ratings (Recommend.All) are not available for this venue.")

_EMA_PERIODS = (9, 10, 20, 30, 50, 100, 200)
_SMA_PERIODS = (10, 20, 30, 50, 100, 200)
_PERP_KEYS = ("mark_price", "prev_day_price", "change_pct_24h", "funding_rate_1h",
              "funding_annualized_pct", "open_interest_usd", "volume_24h_usd")


def _px(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    if abs(x) >= 1:
        return round(x, 4)
    return float(f"{x:.6g}")


def _sig6(x: Optional[float]) -> Optional[float]:
    return None if x is None else float(f"{x:.6g}")


def _rnd(x: Optional[float], n: int) -> Optional[float]:
    return None if x is None else round(x, n)


def _no_data(message: str) -> ScreenerServiceError:
    return ScreenerServiceError(ErrorCode.NO_DATA, message,
                                exchange="HYPERLIQUID", retryable=False)


def _last(values: list) -> Optional[float]:
    return values[-1] if values else None


def indicator_snapshot(candles: list[dict]) -> dict:
    """TradingView-named indicator dict from candles (unrounded; None = too little history)."""
    if not candles:
        raise _no_data("No candles available to compute indicators.")
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]
    last = candles[-1]

    s: dict = {k: last[k] for k in ("open", "high", "low", "close", "volume")}
    s["RSI"] = _last(_calc.calc_rsi(closes, 14))
    macd = _calc.calc_macd(closes, 12, 26, 9)
    s["MACD.macd"] = _last(macd["macd"])
    s["MACD.signal"] = _last(macd["signal"])
    for n in _EMA_PERIODS:
        s[f"EMA{n}"] = _last(_calc.calc_ema(closes, n))
    for n in _SMA_PERIODS:
        s[f"SMA{n}"] = _last(_calc.calc_sma(closes, n))
    bb = _calc.calc_bollinger(closes, 20, 2.0)
    s["BB.upper"] = _last(bb["upper"])
    s["BB.lower"] = _last(bb["lower"])
    s["ATR"] = _last(_calc.calc_atr(highs, lows, closes, 14))
    s["volume.SMA20"] = _last(_calc.calc_sma(volumes, 20))
    st = _calc.calc_supertrend(highs, lows, closes, 10, 3.0)
    s["Supertrend.dir"] = _last(st["direction"])
    s["Supertrend.upper"] = _last(st["upper"])
    s["Supertrend.lower"] = _last(st["lower"])
    return s


def _sections(s: dict) -> dict:
    """Shared per-timeframe sections built from an indicator snapshot."""
    close, open_ = s["close"], s["open"]
    change = round((close - open_) / open_ * 100, 3) if open_ else 0.0

    rsi_v = s["RSI"]
    rsi_signal = "Neutral"
    if rsi_v is not None:
        if rsi_v > 70:
            rsi_signal = "Overbought"
        elif rsi_v > 60:
            rsi_signal = "Bullish"
        elif rsi_v < 30:
            rsi_signal = "Oversold"
        elif rsi_v < 40:
            rsi_signal = "Bearish"
    rsi = {"value": _rnd(rsi_v, 2), "signal": rsi_signal}

    m_line, m_sig = s["MACD.macd"], s["MACD.signal"]
    crossover, hist = "Neutral", None
    if m_line is not None and m_sig is not None:
        hist = m_line - m_sig
        if m_line > m_sig:
            crossover = "Bullish"
        elif m_line < m_sig:
            crossover = "Bearish"
    macd = {"macd_line": _sig6(m_line), "signal_line": _sig6(m_sig),
            "histogram": _sig6(hist), "crossover": crossover}

    sma = {f"sma{n}": _px(s[f"SMA{n}"]) for n in _SMA_PERIODS}
    ema = {f"ema{n}": _px(s[f"EMA{n}"]) for n in _EMA_PERIODS}

    upper, lower, middle = s["BB.upper"], s["BB.lower"], s["SMA20"]
    bbw = None
    if upper and lower and middle and middle > 0:
        bbw = (upper - lower) / middle
    if close and upper and lower:
        if close > upper:
            position = "Above Upper Band"
        elif close < lower:
            position = "Below Lower Band"
        elif middle and close > middle:
            position = "Upper Half"
        else:
            position = "Lower Half"
    else:
        position = "Unknown"
    bollinger = {"upper": _px(upper), "middle": _px(middle), "lower": _px(lower),
                 "width": _rnd(bbw, 4), "squeeze": bbw is not None and bbw < 0.02,
                 "position": position}

    atr_v = s["ATR"]
    atr_pct = (atr_v / close * 100) if atr_v is not None and close and close > 0 else None
    atr = {"value": _px(atr_v), "percent_of_price": _rnd(atr_pct, 2),
           "volatility": ("Unknown" if atr_pct is None else "High" if atr_pct > 3
                          else "Medium" if atr_pct > 1.5 else "Low")}

    vol, vsma = s["volume"], s["volume.SMA20"]
    ratio, vsig = None, "Normal"
    if vol is not None and vsma and vsma > 0:
        ratio = vol / vsma
        if ratio >= 3.0:
            vsig = "Very High"
        elif ratio >= 2.0:
            vsig = "High"
        elif ratio >= 1.5:
            vsig = "Above Average"
        elif ratio < 0.5:
            vsig = "Very Low"
        elif ratio < 0.8:
            vsig = "Below Average"
    volume_analysis = {"current": round(vol, 4), "average_20": _rnd(vsma, 4),
                       "ratio": _rnd(ratio, 2), "signal": vsig}

    d = s["Supertrend.dir"]
    supertrend = {"direction": "Bullish" if d == 1 else "Bearish" if d == -1 else None,
                  "upper": _px(s["Supertrend.upper"]), "lower": _px(s["Supertrend.lower"])}

    score = 0
    for key in ("EMA20", "EMA50", "EMA200"):
        v = s[key]
        if v is None:
            continue
        score += 1 if close > v else -1 if close < v else 0
    trend = "Bullish" if score >= 2 else "Bearish" if score <= -2 else "Neutral/Ranging"
    structure = {"trend": trend, "trend_score": score}

    return {"change": change, "rsi": rsi, "macd": macd, "sma": sma, "ema": ema,
            "bollinger_bands": bollinger, "atr": atr, "volume_analysis": volume_analysis,
            "supertrend": supertrend, "market_structure": structure, "bbw": bbw}


def _sentiment(s: dict, sec: dict) -> dict:
    upper, lower, middle = s["BB.upper"], s["BB.lower"], s["SMA20"]
    if upper is not None and lower is not None and middle is not None:
        rating, signal = _ind.compute_bb_rating_signal(s["close"], upper, middle, lower)
    else:
        rating, signal = None, "N/A"
    bbw = sec["bbw"]
    return {"overall_rating": rating, "buy_sell_signal": signal,
            "volatility": ("Unknown" if bbw is None else "High" if bbw > 0.05
                           else "Medium" if bbw > 0.02 else "Low"),
            "momentum": "Bullish" if sec["change"] > 0 else "Bearish"}


def build_coin_analysis(market: dict, timeframe: str, candle_result: dict) -> dict:
    candles = candle_result["candles"]
    s = indicator_snapshot(candles)
    sec = _sections(s)
    coin = market["coin"]
    close = s["close"]
    last_bar = datetime.fromtimestamp(candles[-1]["t"] / 1000, tz=timezone.utc)
    return {
        "symbol": hyperliquid_symbols.to_coinlist_line(coin), "coin": coin,
        "exchange": "HYPERLIQUID", "source": "hyperliquid",
        "timeframe": timeframe, "interval": candle_result["interval"],
        "bars_used": len(candles),
        "last_bar_time": last_bar.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price_data": {"current_price": _px(close), "open": _px(s["open"]),
                       "high": _px(s["high"]), "low": _px(s["low"]), "close": _px(close),
                       "change_percent": sec["change"], "volume": round(s["volume"], 4)},
        "timeframe_context": _ind.analyze_timeframe_context(s, timeframe),
        "rsi": sec["rsi"], "macd": sec["macd"], "sma": sec["sma"], "ema": sec["ema"],
        "bollinger_bands": sec["bollinger_bands"], "atr": sec["atr"],
        "volume_analysis": sec["volume_analysis"], "supertrend": sec["supertrend"],
        "market_structure": sec["market_structure"],
        "market_sentiment": _sentiment(s, sec),
        "perp_context": {k: market[k] for k in _PERP_KEYS},
        "not_available": list(NOT_AVAILABLE), "note": NOTE,
    }


def build_tf_entry(timeframe: str, candles: list[dict]) -> tuple[dict, int]:
    s = indicator_snapshot(candles)
    sec = _sections(s)
    ctx = _ind.analyze_timeframe_context(s, timeframe)
    bias_num = 1 if ctx["bias"] == "Bullish" else -1 if ctx["bias"] == "Bearish" else 0
    entry = {
        "label": MTF_LABELS[timeframe], "bias": ctx["bias"],
        "bias_reasons": ctx["bias_reasons"],
        "key_indicators": ctx["key_indicators_for_timeframe"], "advice": ctx["advice"],
        "price": _px(s["close"]), "change_pct": sec["change"],
        "rsi": sec["rsi"], "macd_crossover": sec["macd"]["crossover"],
        "ema_trend": {"ema20": _px(s["EMA20"]), "ema50": _px(s["EMA50"]),
                      "ema200": _px(s["EMA200"])},
        "volume_signal": sec["volume_analysis"]["signal"],
        "market_structure": sec["market_structure"]["trend"],
        "supertrend": sec["supertrend"]["direction"],
        "bars_used": len(candles),
    }
    return entry, bias_num


def build_alignment(alignment_scores: list[tuple[str, int]]) -> tuple[dict, dict]:
    total_score = sum(s for _, s in alignment_scores)
    all_bullish = all(s > 0 for _, s in alignment_scores) if alignment_scores else False
    all_bearish = all(s < 0 for _, s in alignment_scores) if alignment_scores else False

    if all_bullish:
        status, confidence, action = "FULLY ALIGNED BULLISH", "Very High", "STRONG BUY - All timeframes bullish. Look for pullback entry on 1H/15m."
    elif all_bearish:
        status, confidence, action = "FULLY ALIGNED BEARISH", "Very High", "STRONG SELL - All timeframes bearish. Avoid longs."
    elif total_score >= 3:
        status, confidence, action = "MOSTLY BULLISH", "High", "BUY - Majority of timeframes bullish. Enter on 4H/1H pullback to support."
    elif total_score <= -3:
        status, confidence, action = "MOSTLY BEARISH", "High", "SELL - Majority of timeframes bearish. Avoid catching the falling knife."
    elif total_score > 0:
        status, confidence, action = "LEAN BULLISH", "Medium", "CAUTIOUS BUY - Some bullish signals but not fully aligned. Wait for better setup."
    elif total_score < 0:
        status, confidence, action = "LEAN BEARISH", "Medium", "CAUTIOUS SELL - Some bearish signals. Reduce position or wait."
    else:
        status, confidence, action = "MIXED/RANGING", "Low", "HOLD/NO TRADE - Timeframes conflict. Wait for alignment."

    higher_tf_bias = alignment_scores[0][1] if alignment_scores else 0
    divergent = [tf for tf, score in alignment_scores
                 if score != 0 and score != higher_tf_bias and higher_tf_bias != 0]
    alignment = {"status": status, "confidence": confidence, "net_score": total_score,
                 "scores_by_tf": dict(alignment_scores), "divergent_timeframes": divergent}
    recommendation = {
        "action": action,
        "entry_timeframe": "1H or 4H pullback" if total_score > 0 else "Wait for alignment",
        "rules": [
            "Weekly sets BIAS (direction only, not entries)",
            "Daily finds SETUP (swing level, confluence)",
            "4H refines entry zone",
            "1H/15m triggers entry with tight stop",
            "Never trade against Weekly + Daily combined direction",
        ],
    }
    return alignment, recommendation


def analyze_hyperliquid_coin(symbol: str, timeframe: str) -> dict:
    """SYNC: full TA snapshot for one Hyperliquid perp."""
    client = hl_svc.get_client()
    market = client.get_market(symbol)
    res = client.candles(market["coin"], timeframe, bars=TA_BARS)
    if not res["candles"]:
        raise _no_data(f"No {timeframe} candles for {market['coin']} on Hyperliquid.")
    return build_coin_analysis(market, timeframe, res)


async def analyze_hyperliquid_mtf(symbol: str) -> dict:
    """ASYNC: multi-timeframe alignment for one Hyperliquid perp."""
    client = hl_svc.get_async_client()
    coin = await client.resolve_coin(symbol)
    results = await asyncio.gather(
        *(client.candles(coin, tf, bars=TA_BARS) for tf in MTF_TIMEFRAMES),
        return_exceptions=True)

    timeframes: dict = {}
    scores: list[tuple[str, int]] = []
    first_err: Optional[ScreenerServiceError] = None
    for tf, res in zip(MTF_TIMEFRAMES, results):
        if isinstance(res, BaseException):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if first_err is None and isinstance(res, ScreenerServiceError):
                first_err = res
            timeframes[tf] = {"error": str(res)}
        elif not res["candles"]:
            timeframes[tf] = {"error": f"No data for {tf}"}
        else:
            entry, bias_num = build_tf_entry(tf, res["candles"])
            timeframes[tf] = entry
            scores.append((tf, bias_num))

    if not scores:
        if first_err is not None:
            raise first_err
        raise _no_data(f"No candles for {coin} on any timeframe.")

    alignment, recommendation = build_alignment(scores)
    return {
        "symbol": hyperliquid_symbols.to_coinlist_line(coin), "coin": coin,
        "exchange": "HYPERLIQUID", "source": "hyperliquid",
        "analysis_type": "Multi-Timeframe Alignment",
        "timeframes": timeframes,
        "alignment": alignment, "recommendation": recommendation,
        "not_available": list(NOT_AVAILABLE),
    }
