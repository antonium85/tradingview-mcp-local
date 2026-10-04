"""Re-capture Hyperliquid fixtures (live network). Not a test."""
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
meta, ctxs = post({"type": "metaAndAssetCtxs"})
rows = list(zip(meta["universe"], ctxs))            # index-aligned; keep the pairs together
live = [r for r in rows if not r[0].get("isDelisted")]
keep = {"BTC", "ETH", "SOL", "HYPE", "kPEPE", "kBONK", "kSHIB", "MATIC", "RNDR"}
by_funding = sorted(live, key=lambda r: float(r[1]["funding"]))
keep |= {u["name"] for u, _ in by_funding[:3] + by_funding[-3:]}
def chg(r):
    p = float(r[1]["prevDayPx"] or 0)
    return float(r[1]["markPx"]) / p - 1 if p else 0.0
keep |= {u["name"] for u, _ in sorted(live, key=chg)[:3]}
sub = [r for r in rows if r[0]["name"] in keep]     # preserves universe order
assert any(u.get("isDelisted") for u, _ in sub), "need a delisted market"
assert any(u["name"].startswith("k") for u, _ in sub), "need a k-prefixed coin"
assert any(float(c["funding"]) < 0 for u, c in sub if not u.get("isDelisted")), "need negative funding"
dump("meta_and_asset_ctxs.json",
     [{"universe": [u for u, _ in sub], "marginTables": [], "collateralToken": meta.get("collateralToken")},
      [c for _, c in sub]])

def candles(coin, iv, n, ms):
    body = {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": iv, "startTime": now - n * ms, "endTime": now}}
    return post(body)[-n:]

dump("candle_snapshot_BTC_1h.json", candles("BTC", "1h", 200, 3_600_000))
dump("candle_snapshot_kPEPE_1d.json", candles("kPEPE", "1d", 60, 86_400_000))
dump("candle_snapshot_MATIC_1h_empty.json", candles("MATIC", "1h", 24, 3_600_000))   # delisted -> []
dump("funding_history_BTC.json",
     post({"type": "fundingHistory", "coin": "BTC", "startTime": now - 72 * 3_600_000})[-72:])
dump("l2_book_BTC.json", post({"type": "l2Book", "coin": "BTC"}))
dump("l2_book_unknown_null.json", post({"type": "l2Book", "coin": "NOTACOIN"}))      # -> null
dump("capture_info.json", {"captured_at_ms": now, "url": URL, "coins": sorted(keep)})
