"""URL building, HTTPS verification, session ownership and secret hygiene."""

import logging
import ssl
from typing import Any

import aiohttp
import pytest
import trustme
from conftest import (
    PASSWORD,
    TOKEN_1,
    TOKEN_2,
    USERNAME,
    FakeUbus,
    body,
    login_body,
    start_server,
)

from aioubus import UbusClient, UbusError, UbusSSLError


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "http://192.168.1.1/ubus"),
        ({"scheme": "https"}, "https://192.168.1.1/ubus"),
        ({"scheme": "https", "port": 8443}, "https://192.168.1.1:8443/ubus"),
        ({"port": 8080, "path": "rpc"}, "http://192.168.1.1:8080/rpc"),
        ({"scheme": "http", "port": 80}, "http://192.168.1.1/ubus"),
    ],
)
async def test_url(kwargs: dict[str, Any], expected: str) -> None:
    async with UbusClient("192.168.1.1", "u", "p", **kwargs) as client:
        assert client.url == expected


async def test_url_ipv6_host() -> None:
    async with UbusClient("fd00::1", "u", "p", scheme="https") as client:
        assert client.url == "https://[fd00::1]/ubus"


@pytest.mark.parametrize("kwargs", [{"scheme": "ftp"}, {"timeout": 0}, {"timeout": -1}])
def test_invalid_arguments(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"scheme|timeout"):
        UbusClient("h", "u", "p", **kwargs)


async def test_https_self_signed_rejected_when_verifying(server_ssl: ssl.SSLContext) -> None:
    fake = FakeUbus("https")
    runner = await start_server(fake, server_ssl)
    try:
        async with UbusClient("127.0.0.1", "u", "p", scheme="https", port=fake.port) as client:
            with pytest.raises(UbusSSLError):
                await client.login()
        assert fake.requests == []
    finally:
        await runner.cleanup()


async def test_https_trusted_via_ssl_context(ca: trustme.CA, server_ssl: ssl.SSLContext) -> None:
    fake = FakeUbus("https")
    fake.respond(login_body())
    runner = await start_server(fake, server_ssl)
    ctx = ssl.create_default_context()
    ca.configure_trust(ctx)
    try:
        async with UbusClient(
            "127.0.0.1", "u", "p", scheme="https", port=fake.port, ssl_context=ctx
        ) as client:
            await client.login()
            assert client.logged_in
    finally:
        await runner.cleanup()


async def test_https_verify_disabled(server_ssl: ssl.SSLContext) -> None:
    fake = FakeUbus("https")
    fake.respond(login_body())
    runner = await start_server(fake, server_ssl)
    try:
        async with UbusClient(
            "127.0.0.1", "u", "p", scheme="https", port=fake.port, verify_ssl=False
        ) as client:
            await client.login()
    finally:
        await runner.cleanup()


async def test_injected_session_is_used_and_not_closed(ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    async with aiohttp.ClientSession() as session:
        async with UbusClient(
            "127.0.0.1",
            "u",
            "p",
            session=session,
            scheme=ubus.scheme,
            port=ubus.port,
            verify_ssl=False,
        ) as client:
            await client.login()
        assert not session.closed
        # The shared session stays usable after the client is closed.
        ubus.respond(login_body())
        await client.login()


async def test_owned_session_is_closed(ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    client = UbusClient(
        "127.0.0.1",
        "u",
        "p",
        scheme=ubus.scheme,
        port=ubus.port,
        verify_ssl=False,
    )
    await client.login()
    session = client._session
    assert session is not None
    await client.close()
    assert session.closed
    assert not client.logged_in
    with pytest.raises(UbusError):
        await client.login()


async def test_close_without_requests() -> None:
    client = UbusClient("h", "u", "p")
    await client.close()
    await client.close()


async def test_request_is_json(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    await client.login()
    assert ubus.headers[0]["Content-Type"] == "application/json"


async def test_request_ids_increment(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    ubus.respond(body("system_board"))
    await client.get_system_board()
    assert [r["id"] for r in ubus.requests] == [1, 2]


async def test_no_secrets_in_logs_reprs_or_errors(
    client: UbusClient, ubus: FakeUbus, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ubus.respond(login_body(TOKEN_1))
    ubus.respond(body("system_board"))
    ubus.respond(body("call_expired_session"))
    ubus.respond(login_body(TOKEN_2))
    ubus.respond(body("acl_denied_object"))
    ubus.respond(body("login_wrong_password"))

    info = await client.login()
    await client.get_system_board()
    with pytest.raises(UbusError) as excinfo:
        await client.call("luci", "setLocaltime", localtime=0)

    texts = [
        caplog.text,
        repr(client),
        str(client),
        repr(info),
        str(excinfo.value),
        repr(excinfo.value),
    ]
    for text in texts:
        assert PASSWORD not in text
        assert TOKEN_1 not in text
        assert TOKEN_2 not in text
    assert USERNAME in repr(client)
    assert "session.login" in caplog.text
