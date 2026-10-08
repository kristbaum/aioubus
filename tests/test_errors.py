"""Mapping of ubus statuses, JSON-RPC errors and transport failures."""

import json

import aiohttp
import pytest
from conftest import RELEASES, FakeUbus, Scheme, body, login_body, result, rpc_error

from aioubus import (
    UbusCallError,
    UbusClient,
    UbusConnectionError,
    UbusError,
    UbusHttpError,
    UbusInvalidArgumentError,
    UbusMethodNotFoundError,
    UbusNoDataError,
    UbusNotFoundError,
    UbusNotSupportedError,
    UbusObjectNotFoundError,
    UbusPermissionError,
    UbusResponseError,
    UbusSSLError,
    UbusStatus,
    UbusTimeoutError,
)

STATUS_ERRORS: list[tuple[int, type[UbusError]]] = [
    (UbusStatus.INVALID_COMMAND, UbusInvalidArgumentError),
    (UbusStatus.INVALID_ARGUMENT, UbusInvalidArgumentError),
    (UbusStatus.METHOD_NOT_FOUND, UbusMethodNotFoundError),
    (UbusStatus.NOT_FOUND, UbusNotFoundError),
    (UbusStatus.NO_DATA, UbusNoDataError),
    (UbusStatus.PERMISSION_DENIED, UbusPermissionError),
    (UbusStatus.TIMEOUT, UbusTimeoutError),
    (UbusStatus.NOT_SUPPORTED, UbusNotSupportedError),
    (UbusStatus.UNKNOWN_ERROR, UbusCallError),
    (UbusStatus.CONNECTION_FAILED, UbusCallError),
    (UbusStatus.NO_MEMORY, UbusCallError),
    (UbusStatus.PARSE_ERROR, UbusCallError),
    (UbusStatus.SYSTEM_ERROR, UbusCallError),
    (99, UbusCallError),
]


def test_status_table_covers_every_nonzero_status() -> None:
    mapped = {status for status, _ in STATUS_ERRORS}
    assert {s for s in UbusStatus if s != UbusStatus.OK} <= mapped


@pytest.mark.parametrize(("status", "error"), STATUS_ERRORS)
async def test_status_maps_to_exception(
    client: UbusClient, ubus: FakeUbus, status: int, error: type[UbusError]
) -> None:
    ubus.respond(login_body())
    ubus.respond(result(status))

    with pytest.raises(error) as excinfo:
        await client.call("luci-rpc", "getHostHints")

    assert type(excinfo.value) is error
    assert excinfo.value.status == status


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize(
    ("fixture", "error", "status"),
    [
        ("method_not_found", UbusMethodNotFoundError, 3),
        ("file_read_missing", UbusNotFoundError, 4),
        ("invalid_argument", UbusInvalidArgumentError, 2),
    ],
)
async def test_captured_status_errors(
    client: UbusClient,
    ubus: FakeUbus,
    release: str,
    fixture: str,
    error: type[UbusError],
    status: int,
) -> None:
    ubus.respond(login_body(release=release))
    ubus.respond(body(fixture, release))

    with pytest.raises(error) as excinfo:
        await client.call("x", "y")

    assert excinfo.value.status == status


@pytest.mark.parametrize("release", RELEASES)
async def test_captured_object_not_found(client: UbusClient, ubus: FakeUbus, release: str) -> None:
    """e.g. rpcd-mod-luci not installed: uhttpd answers -32000."""
    ubus.respond(login_body(release=release))
    ubus.respond(body("object_not_found", release))

    with pytest.raises(UbusObjectNotFoundError) as excinfo:
        await client.get_host_hints()

    assert excinfo.value.rpc_code == -32000
    assert isinstance(excinfo.value, UbusNotFoundError)


async def test_captured_wireless_absent_is_not_found(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    ubus.respond(body("luci_getWirelessDevices"))

    with pytest.raises(UbusNotFoundError):
        await client.get_wireless_devices()


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (-32700, UbusCallError),
        (-32600, UbusCallError),
        (-32601, UbusCallError),
        (-32602, UbusInvalidArgumentError),
        (-32603, UbusCallError),
        (-32003, UbusTimeoutError),
        (-1, UbusCallError),
    ],
)
async def test_jsonrpc_error_maps_to_exception(
    client: UbusClient, ubus: FakeUbus, code: int, error: type[UbusError]
) -> None:
    ubus.respond(login_body())
    ubus.respond(rpc_error(code, "boom"))

    with pytest.raises(error) as excinfo:
        await client.call("a", "b")

    assert excinfo.value.rpc_code == code


@pytest.mark.parametrize("http_status", [400, 403, 404, 500, 502])
async def test_http_error_status(client: UbusClient, ubus: FakeUbus, http_status: int) -> None:
    ubus.respond("<html>error</html>", status=http_status)

    with pytest.raises(UbusHttpError) as excinfo:
        await client.login()

    assert excinfo.value.http_status == http_status
    assert isinstance(excinfo.value, UbusConnectionError)


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (aiohttp.ClientConnectionError("refused"), UbusConnectionError),
        (aiohttp.ServerDisconnectedError(), UbusConnectionError),
        (aiohttp.ClientPayloadError("truncated"), UbusConnectionError),
        (TimeoutError(), UbusTimeoutError),
        (aiohttp.ServerTimeoutError("read timeout"), UbusTimeoutError),
    ],
)
async def test_transport_exceptions_are_wrapped(
    client: UbusClient,
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    error: type[UbusError],
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(aiohttp.ClientSession, "post", fail)

    with pytest.raises(error) as excinfo:
        await client.login()

    assert type(excinfo.value) is error
    assert excinfo.value.__cause__ is exc


async def test_real_timeout(ubus: FakeUbus) -> None:
    ubus.respond(login_body(), delay=2)
    async with UbusClient(
        "127.0.0.1",
        "root",
        "x",
        scheme=ubus.scheme,
        port=ubus.port,
        verify_ssl=False,
        timeout=0.2,
    ) as client:
        with pytest.raises(UbusTimeoutError):
            await client.login()


async def test_real_connection_refused(unused_tcp_port: int, scheme: Scheme) -> None:
    async with UbusClient(
        "127.0.0.1",
        "root",
        "x",
        scheme=scheme,
        port=unused_tcp_port,
    ) as client:
        with pytest.raises(UbusConnectionError):
            await client.login()


async def test_real_disconnect_mid_session(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    ubus.respond("", disconnect=True)

    with pytest.raises(UbusConnectionError):
        await client.get_host_hints()


async def test_wrong_endpoint_path(ubus: FakeUbus) -> None:
    async with UbusClient(
        "127.0.0.1",
        "root",
        "x",
        scheme=ubus.scheme,
        port=ubus.port,
        path="/cgi-bin/luci/rpc",
        verify_ssl=False,
    ) as client:
        with pytest.raises(UbusHttpError) as excinfo:
            await client.login()
    assert excinfo.value.http_status == 404


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{not json",
        '{"jsonrpc":"2.0","id":1,"result":[0,{"ipaddrs":',  # truncated
        "\xff\xfe",
        "<html><body>LuCI</body></html>",
    ],
)
async def test_malformed_json(client: UbusClient, ubus: FakeUbus, raw: str) -> None:
    ubus.respond(raw)

    with pytest.raises(UbusResponseError):
        await client.login()


@pytest.mark.parametrize(
    "envelope",
    [
        [],
        "string",
        42,
        None,
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "result": []},
        {"jsonrpc": "2.0", "id": 1, "result": ["0"]},
        {"jsonrpc": "2.0", "id": 1, "result": [True]},
        {"jsonrpc": "2.0", "id": 1, "result": [None, {}]},
        {"jsonrpc": "2.0", "id": 1, "result": [0, []]},
        {"jsonrpc": "2.0", "id": 1, "result": [0, "data"]},
        {"jsonrpc": "2.0", "id": 1, "error": "boom"},
        {"jsonrpc": "2.0", "id": 1, "error": {"message": "no code"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": "x"}},
    ],
)
async def test_unexpected_envelope_shape(
    client: UbusClient, ubus: FakeUbus, envelope: object
) -> None:
    ubus.respond(login_body())
    ubus.respond(json.dumps(envelope))

    with pytest.raises(UbusResponseError):
        await client.call("a", "b")


@pytest.mark.parametrize(
    "payload",
    [
        {"timeout": 300, "expires": 299},  # no token
        {"ubus_rpc_session": 5, "timeout": 300, "expires": 299},
        {"ubus_rpc_session": "a" * 32, "expires": 299},  # no timeout
        {"ubus_rpc_session": "a" * 32, "timeout": "300", "expires": 299},
        {"ubus_rpc_session": "a" * 32, "timeout": 300, "expires": 299, "acls": []},
        {
            "ubus_rpc_session": "a" * 32,
            "timeout": 300,
            "expires": 299,
            "acls": {"ubus": {"x": "y"}},
        },
    ],
)
async def test_unexpected_login_payload(
    client: UbusClient, ubus: FakeUbus, payload: object
) -> None:
    ubus.respond(result(0, payload))

    with pytest.raises(UbusResponseError):
        await client.login()
    assert not client.logged_in


async def test_login_without_payload(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(result(0))

    with pytest.raises(UbusResponseError):
        await client.login()


@pytest.mark.parametrize(
    "fixture", ["malformed_request_json", "request_wrong_jsonrpc_version", "unknown_rpc_method"]
)
async def test_captured_request_level_errors(
    client: UbusClient, ubus: FakeUbus, fixture: str
) -> None:
    """Captured uhttpd answers to bad requests surface as UbusCallError."""
    ubus.respond(login_body())
    ubus.respond(body(fixture))

    with pytest.raises(UbusCallError) as excinfo:
        await client.call("a", "b")

    assert excinfo.value.rpc_code in (-32700, -32601)


def test_all_errors_share_base() -> None:
    for error in (
        UbusConnectionError,
        UbusTimeoutError,
        UbusSSLError,
        UbusHttpError,
        UbusPermissionError,
        UbusResponseError,
        UbusCallError,
        UbusNotFoundError,
    ):
        assert issubclass(error, UbusError)
