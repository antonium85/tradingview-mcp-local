"""Pure Hyperliquid symbol resolution (no I/O, no provider imports).

Hyperliquid coin names are case-sensitive (``kPEPE``) but the shared validators
uppercase every symbol, so resolution goes through a case-insensitive index
that maps back to the canonical name.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping

from tradingview_mcp.core.errors import ErrorCode, ScreenerServiceError

HYPERLIQUID_PREFIX: str = "HYPERLIQUID"
PERP_SUFFIXES: tuple[str, ...] = (".P", "-PERP", "_PERP", "/PERP", "PERP")
QUOTE_SUFFIXES: tuple[str, ...] = ("USDT", "USDC", "USDH", "USDE", "USD")  # longest first
QUOTE_SEPARATORS: tuple[str, ...] = ("-", "/", "_")


def _invalid(raw: str, message: str) -> ScreenerServiceError:
    return ScreenerServiceError(
        ErrorCode.INVALID_PARAMETER,
        message,
        symbol=raw,
        exchange="HYPERLIQUID",
        retryable=False,
    )


def _strip_one(s: str, suffixes: tuple[str, ...]) -> str:
    for suf in suffixes:
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


def candidate_coin_names(raw: str) -> list[str]:
    """Return UPPERCASE coin-name candidates for ``raw``, most literal first."""
    s = (raw or "").strip()
    if ":" in s:
        prefix, rest = s.split(":", 1)
        if prefix.strip().upper() != HYPERLIQUID_PREFIX:
            raise _invalid(
                raw, "Only 'HYPERLIQUID:' prefixed or bare coin names are accepted."
            )
        s = rest
    c0 = s.strip().upper()
    c1 = _strip_one(c0, PERP_SUFFIXES)
    c2 = _strip_one(c1, QUOTE_SUFFIXES)
    c2 = _strip_one(c2, QUOTE_SEPARATORS)
    if not c2:
        # Nothing left once suffixes are stripped ("", "HYPERLIQUID:", "-PERP").
        raise _invalid(raw, f"{raw!r} does not contain a coin name.")

    out: list[str] = []
    for c in (c0, c1, c2):
        if c not in out:
            out.append(c)
    expanded: list[str] = []
    for c in out:
        expanded.append(c)
        if c.startswith("1000") and len(c) > 4:
            expanded.append("K" + c[4:])
    result: list[str] = []
    for c in expanded:
        # Drop empties and separator-only leftovers such as "-" from "-PERP".
        if c and any(ch.isalnum() for ch in c) and c not in result:
            result.append(c)
    if not result:
        raise _invalid(raw, f"{raw!r} does not contain a coin name.")
    return result


def build_name_index(names: Iterable[str]) -> dict[str, str]:
    """Map ``NAME.upper()`` to the canonical name; reject case collisions."""
    index: dict[str, str] = {}
    for name in names:
        key = name.upper()
        if key in index and index[key] != name:
            raise ValueError(
                f"Hyperliquid names {index[key]!r} and {name!r} collide when upper-cased"
            )
        index[key] = name
    return index


def resolve_coin(
    raw: str,
    live: Mapping[str, str],
    delisted: Mapping[str, str] | None = None,
) -> str:
    """Resolve ``raw`` to a canonical live coin name or raise SYMBOL_NOT_FOUND."""
    candidates = candidate_coin_names(raw)
    for c in candidates:
        if c in live:
            return live[c]
    if delisted:
        for c in candidates:
            if c in delisted:
                raise ScreenerServiceError(
                    ErrorCode.SYMBOL_NOT_FOUND,
                    f"{delisted[c]} is delisted on Hyperliquid.",
                    symbol=raw,
                    exchange="HYPERLIQUID",
                    delisted=True,
                    retryable=False,
                )
    close = difflib.get_close_matches(candidates[-1], list(live.keys()), n=5, cutoff=0.6)
    raise ScreenerServiceError(
        ErrorCode.SYMBOL_NOT_FOUND,
        f"{raw!r} is not a listed Hyperliquid perpetual.",
        symbol=raw,
        exchange="HYPERLIQUID",
        suggestions=[live[k] for k in close],
        retryable=False,
    )


def to_coinlist_line(coin: str) -> str:
    """``"kPEPE"`` -> ``"HYPERLIQUID:kPEPE"``."""
    return f"{HYPERLIQUID_PREFIX}:{coin}"
