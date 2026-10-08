"""Shared fixtures.

Fixture files under ``tests/fixtures/openwrt-*`` are raw HTTP bodies captured
from real OpenWrt releases (see ``scripts/capture_fixtures.py``);
``openwrt-25.12.5-hwsim`` comes from the QEMU lab in ``real_test/``, with
virtual radios and associated stations. Files under
``tests/fixtures/derived`` were written from the cited upstream source where
no live capture was possible.

Tests talk to :class:`FakeUbus`, a real local HTTP(S) server that replays
queued response bodies in order and records each request body.
"""

import asyncio
import json
import re
import ssl
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import trustme
from aiohttp import web

from aioubus import UbusClient

FIXTURES = Path(__file__).parent / "fixtures"
LATEST = "openwrt-25.12.5"
RELEASES = ("openwrt-25.12.5", "openwrt-24.10.8", "openwrt-24.10.8-nginx")

USERNAME = "root"
PASSWORD = "s3cret-Pa55word"
# Placeholder tokens written by the capture script.
TOKEN_1 = "00000000000000000000000000000001"
TOKEN_2 = "0000000000000000000000000000beef"

type Scheme = Literal["http", "https"]

_TOKEN_RE = re.compile(r'"ubus_rpc_session":"[0-9a-f]{32}"')


def body(name: str, release: str = LATEST) -> str:
    """Return the raw HTTP body of a fixture."""
    data = json.loads((FIXTURES / release / f"{name}.json").read_text())
    text: str = data["body"]
    return text


def derived(name: str) -> str:
    return body(name, "derived")


def login_body(token: str = TOKEN_1, release: str = LATEST) -> str:
    """The captured successful login, with its token replaced."""
    return _TOKEN_RE.sub(f'"ubus_rpc_session":"{token}"', body("login_ok", release))


def result(*items: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": list(items)})


def rpc_error(code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


@dataclass
class _Response:
    body: str | bytes
    status: int
    delay: float
    repeat: bool
    disconnect: bool


class FakeUbus:
    """A real HTTP(S) server that replays queued ubus responses."""

    def __init__(self, scheme: Scheme, path: str = "/ubus") -> None:
        self.scheme = scheme
        self.path = path
        self.requests: list[Any] = []
        self.headers: list[dict[str, str]] = []
        self._queue: deque[_Response] = deque()
        self.port = 0
        #: Optional function choosing a response body from the request,
        #: used instead of the queue when set.
        self.route: Callable[[Any], str] | None = None

    def respond(
        self,
        body: str | bytes,
        *,
        status: int = 200,
        delay: float = 0.0,
        repeat: bool = False,
        disconnect: bool = False,
    ) -> None:
        self._queue.append(_Response(body, status, delay, repeat, disconnect))

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        try:
            self.requests.append(json.loads(raw))
        except ValueError:
            self.requests.append(raw)
        self.headers.append(dict(request.headers))
        if self.route is not None:
            reply = self.route(self.requests[-1])
            await asyncio.sleep(0.01)  # let concurrent requests interleave
            return web.Response(body=reply, content_type="application/json")
        if not self._queue:
            return web.Response(status=599, text="no response queued")
        response = self._queue[0] if self._queue[0].repeat else self._queue.popleft()
        if response.delay:
            await asyncio.sleep(response.delay)
        if response.disconnect:
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        return web.Response(
            status=response.status,
            body=response.body,
            content_type="application/json",
        )

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post(self.path, self._handle)
        return app


@pytest.fixture(scope="session")
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture(scope="session")
def server_ssl(ca: trustme.CA) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1", "localhost").configure_cert(ctx)
    return ctx


@pytest.fixture(params=["http", "https"])
def scheme(request: pytest.FixtureRequest) -> Scheme:
    value: Scheme = request.param
    return value


async def start_server(fake: FakeUbus, server_ssl: ssl.SSLContext | None) -> web.AppRunner:
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl)
    await site.start()
    server = site._server
    assert isinstance(server, asyncio.Server)
    fake.port = server.sockets[0].getsockname()[1]
    return runner


@pytest.fixture
async def ubus(scheme: Scheme, server_ssl: ssl.SSLContext) -> AsyncIterator[FakeUbus]:
    fake = FakeUbus(scheme)
    runner = await start_server(fake, server_ssl if scheme == "https" else None)
    yield fake
    await runner.cleanup()


@pytest.fixture
async def client(ubus: FakeUbus) -> AsyncIterator[UbusClient]:
    async with UbusClient(
        "127.0.0.1",
        USERNAME,
        PASSWORD,
        scheme=ubus.scheme,
        port=ubus.port,
        verify_ssl=False,
        timeout=5,
    ) as c:
        yield c
