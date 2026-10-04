#!/usr/bin/env python3
"""Generate Hyperliquid coinlist from live API. Run as: uv run python scripts/gen_hyperliquid_coinlist.py"""
import re
import sys
import httpx
import pathlib

URL = "https://api.hyperliquid.xyz/info"
COINLIST_PATH = pathlib.Path(__file__).parent.parent / "src" / "tradingview_mcp" / "coinlist" / "hyperliquid.txt"

# Regex to validate coin names: only alphanumeric
VALID_NAME = re.compile(r"^[A-Za-z0-9]+$")

def main():
    """Fetch metaAndAssetCtxs and generate the coinlist."""
    try:
        # Fetch the live data
        response = httpx.post(
            URL,
            json={"type": "metaAndAssetCtxs"},
            timeout=10
        )
        response.raise_for_status()

        # Parse response: [meta, ctxs]
        meta, ctxs = response.json()

        # Extract live coins (not delisted) from meta["universe"]
        universe = meta.get("universe", [])
        live_coins = []

        for entry in universe:
            if not entry.get("isDelisted", False):
                name = entry.get("name", "").strip()
                if name:
                    live_coins.append(name)

        # Validate all coin names
        for name in live_coins:
            if not VALID_NAME.match(name):
                print(f"ERROR: Coin name {name!r} does not match ^[A-Za-z0-9]+$", file=sys.stderr)
                return 1

        # Check minimum count
        if len(live_coins) < 100:
            print(f"ERROR: Only {len(live_coins)} live coins, need at least 100", file=sys.stderr)
            return 1

        # Check for case-insensitive duplicates
        seen_upper = {}
        for name in live_coins:
            upper = name.upper()
            if upper in seen_upper:
                print(f"ERROR: Case-insensitive duplicate: {name!r} vs {seen_upper[upper]!r}", file=sys.stderr)
                return 1
            seen_upper[upper] = name

        # Sort by uppercase and format
        sorted_coins = sorted(live_coins, key=str.upper)
        lines = [f"HYPERLIQUID:{name}" for name in sorted_coins]

        # Write with trailing newline
        output = "\n".join(lines) + "\n"
        COINLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        COINLIST_PATH.write_text(output)

        print(f"Generated {len(lines)} Hyperliquid coins to {COINLIST_PATH}")
        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
