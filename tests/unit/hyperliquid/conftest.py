"""Shared fixtures for the Hyperliquid unit tests (no network, real fixtures)."""
from __future__ import annotations

import json
import pathlib
import socket
import threading

import httpx
import pytest

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hyperliquid"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class HLMock:
    """Routes Hyperliquid /info POSTs to saved real responses.

    Unknown coins mirror live behaviour: candleSnapshot/fundingHistory give
    HTTP 500 with body ``null``; l2Book gives 200 ``null``.
    ``script(type, [...])`` queues overrides for one request type; each item
    is an httpx.Response, an Exception (raised) or a callable(request).
    The last scripted item repeats.
    """

    def __init__(self):
        self.meta = load_fixture("meta_and_asset_ctxs.json")
        self.known = {u["name"] for u in self.meta[0]["universe"]}
        self.requests: list[dict] = []
        self.scripts: dict[str, list] = {}
        self.current = 0
        self.peak = 0
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self.handle)

    # -- helpers
    def script(self, req_type: str, items: list) -> None:
        self.scripts[req_type] = list(items)

    def of_type(self, req_type: str) -> list[dict]:
        return [b for b in self.requests if b.get("type") == req_type]

    @property
    def count(self) -> int:
        return len(self.requests)

    @staticmethod
    def coin_of(body: dict):
        return body.get("coin") or (body.get("req") or {}).get("coin")

    # -- handler
    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        with self._lock:
            self.requests.append(body)
            self.current += 1
            self.peak = max(self.peak, self.current)
        try:
            return self.route(request, body)
        finally:
            with self._lock:
                self.current -= 1

    def route(self, request, body):
        t = body.get("type")
        queue = self.scripts.get(t)
        if queue:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(request)
            # Fresh copy per request: a Response's stream is single-use.
            return httpx.Response(item.status_code, headers=item.headers, content=item.content)
        coin = self.coin_of(body)
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=self.meta)
        if t == "candleSnapshot":
            if coin not in self.known:
                return httpx.Response(500, content=b"null")
            if coin in ("MATIC", "RNDR"):
                return httpx.Response(200, json=load_fixture("candle_snapshot_MATIC_1h_empty.json"))
            if coin == "kPEPE":
                return httpx.Response(200, json=load_fixture("candle_snapshot_kPEPE_1d.json"))
            return httpx.Response(200, json=load_fixture("candle_snapshot_BTC_1h.json"))
        if t == "fundingHistory":
            if coin not in self.known:
                return httpx.Response(500, content=b"null")
            return httpx.Response(200, json=load_fixture("funding_history_BTC.json"))
        if t == "l2Book":
            if coin not in self.known:
                return httpx.Response(200, content=b"null")
            book = load_fixture("l2_book_BTC.json")
            book["coin"] = coin  # live API echoes the requested coin
            return httpx.Response(200, json=book)
        return httpx.Response(422, text="Failed to deserialize the JSON body")


@pytest.fixture
def hl_mock():
    return HLMock()


@pytest.fixture(autouse=True)
def _hl_clients(monkeypatch, hl_mock):
    """Point the default clients at the mock; zero backoff; clean state after."""
    monkeypatch.setenv("HYPERLIQUID_MCP_RETRY_DELAYS", "0,0")
    try:
        from tradingview_mcp.core.services import hyperliquid_service as svc
    except ImportError:  # WP-B not landed yet: tests needing it fail on their own import
        yield
        return
    svc.configure_default_clients(transport=hl_mock.transport)
    yield
    monkeypatch.undo()
    svc.reset_state()


@pytest.fixture
def no_network(monkeypatch):
    calls: list[tuple] = []

    def _gai(*a, **k):
        calls.append(("getaddrinfo", a))
        raise OSError("network disabled in unit tests")

    def _connect(self, *a, **k):
        calls.append(("connect", a))
        raise OSError("network disabled in unit tests")

    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    monkeypatch.setattr(socket.socket, "connect", _connect)
    return calls
