"""Capture step-2 candle fixtures (live network). Not a test. Leaves step-1 files alone."""
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

def candles(coin, iv, n, ms):
    body = {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": iv, "startTime": now - n * ms, "endTime": now}}
    return post(body)[-n:]

files = {
    "candle_snapshot_BTC_1d.json":  candles("BTC", "1d", 400, 86_400_000),
    "candle_snapshot_BTC_4h.json":  candles("BTC", "4h", 300, 14_400_000),
    "candle_snapshot_BTC_15m.json": candles("BTC", "15m", 300, 900_000),
    "candle_snapshot_BTC_1w.json":  candles("BTC", "1w", 300, 604_800_000),
}
assert len(files["candle_snapshot_BTC_1d.json"]) == 400
assert len(files["candle_snapshot_BTC_4h.json"]) == 300
assert len(files["candle_snapshot_BTC_15m.json"]) == 300
assert len(files["candle_snapshot_BTC_1w.json"]) >= 200      # live: 301 weekly bars (2026-10-04)
for name, data in files.items():
    dump(name, data)
dump("capture_info_step2.json", {"captured_at_ms": now, "url": URL, "files": sorted(files)})
