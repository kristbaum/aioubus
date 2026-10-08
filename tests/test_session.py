"""Login, session expiry and re-login behaviour."""

import asyncio
from typing import Any

import pytest
from conftest import (
    PASSWORD,
    RELEASES,
    TOKEN_1,
    TOKEN_2,
    USERNAME,
    FakeUbus,
    body,
    login_body,
    result,
    rpc_error,
)

from aioubus import (
    NULL_SESSION_ID,
    JsonRpcError,
    UbusAuthenticationError,
    UbusClient,
    UbusPermissionError,
    UbusStatus,
)


@pytest.mark.parametrize("release", RELEASES)
async def test_login_success(client: UbusClient, ubus: FakeUbus, release: str) -> None:
    ubus.respond(login_body(release=release))

    info = await client.login()

    assert info.timeout == 300
    assert info.expires == 299
    assert "luci-base" in info.acls["access-group"]
    assert info.acls["access-group"]["luci-base"] == ("read", "write")
    assert client.logged_in
    request = ubus.requests[0]
    assert request["method"] == "call"
    assert request["jsonrpc"] == "2.0"
    assert request["params"] == [
        NULL_SESSION_ID,
        "session",
        "login",
        {"username": USERNAME, "password": PASSWORD},
    ]


async def test_login_requests_session_timeout(ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    async with UbusClient(
        "127.0.0.1",
        USERNAME,
        PASSWORD,
        scheme=ubus.scheme,
        port=ubus.port,
        verify_ssl=False,
        session_timeout=600,
    ) as client:
        await client.login()
    assert ubus.requests[0]["params"][3]["timeout"] == 600


@pytest.mark.parametrize("release", RELEASES)
async def test_login_short_timeout(ubus: FakeUbus, release: str) -> None:
    """rpcd grants the requested timeout; the client tracks it for renewal."""
    ubus.respond(body("login_short_timeout", release))
    async with UbusClient(
        "127.0.0.1",
        USERNAME,
        PASSWORD,
        scheme=ubus.scheme,
        port=ubus.port,
        verify_ssl=False,
        session_timeout=1,
    ) as client:
        info = await client.login()
    assert ubus.requests[0]["params"][3]["timeout"] == 1
    assert info.timeout == 1


@pytest.mark.parametrize("fixture", ["login_wrong_password", "login_unknown_user"])
@pytest.mark.parametrize("release", RELEASES)
async def test_login_wrong_credentials(
    client: UbusClient, ubus: FakeUbus, fixture: str, release: str
) -> None:
    ubus.respond(body(fixture, release))

    with pytest.raises(UbusAuthenticationError) as excinfo:
        await client.login()

    assert excinfo.value.status == UbusStatus.PERMISSION_DENIED
    assert not client.logged_in


async def test_login_not_permitted_by_acl(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(rpc_error(-32002, "Access denied"))

    with pytest.raises(UbusAuthenticationError) as excinfo:
        await client.login()

    assert excinfo.value.rpc_code == JsonRpcError.ACCESS_DENIED


async def test_first_call_logs_in_lazily(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    ubus.respond(body("luci_getHostHints"))

    await client.get_host_hints()

    login, call = ubus.requests
    assert login["params"][:3] == [NULL_SESSION_ID, "session", "login"]
    assert call["params"] == [TOKEN_1, "luci-rpc", "getHostHints", {}]


async def test_session_reused_across_calls(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body())
    ubus.respond(body("luci_getHostHints"))
    ubus.respond(body("system_board"))

    await client.get_host_hints()
    await client.get_system_board()

    requests = ubus.requests
    assert len(requests) == 3
    assert [r["params"][0] for r in requests[1:]] == [TOKEN_1, TOKEN_1]


# All three are captured -32002 rejections: an expired session, a token rpcd
# never issued, and the null session ID.
@pytest.mark.parametrize(
    "rejection", ["call_expired_session", "call_bogus_session", "call_null_session"]
)
@pytest.mark.parametrize("release", RELEASES)
async def test_session_expiry_mid_session_relogin_and_retry(
    client: UbusClient, ubus: FakeUbus, release: str, rejection: str
) -> None:
    """Rejected session -> one re-login -> retry succeeds."""
    ubus.respond(login_body(TOKEN_1, release))
    ubus.respond(body("luci_getHostHints", release))
    ubus.respond(body(rejection, release))
    ubus.respond(login_body(TOKEN_2, release))
    ubus.respond(body("luci_getHostHints", release))

    first = await client.get_host_hints()
    second = await client.get_host_hints()

    assert first == second
    requests = ubus.requests
    assert [r["params"][0] for r in requests] == [
        NULL_SESSION_ID,
        TOKEN_1,
        TOKEN_1,
        NULL_SESSION_ID,
        TOKEN_2,
    ]
    assert client.logged_in


async def test_session_not_found_code_also_triggers_relogin(
    client: UbusClient, ubus: FakeUbus
) -> None:
    ubus.respond(login_body(TOKEN_1))
    ubus.respond(rpc_error(-32001, "Session not found"))
    ubus.respond(login_body(TOKEN_2))
    ubus.respond(body("system_board"))

    board = await client.get_system_board()

    assert board.release.version == "25.12.5"


async def test_expiry_then_relogin_rejected_raises_auth_error(
    client: UbusClient, ubus: FakeUbus
) -> None:
    """Password changed while the session was alive."""
    ubus.respond(login_body())
    ubus.respond(body("call_expired_session"))
    ubus.respond(body("login_wrong_password"))

    with pytest.raises(UbusAuthenticationError):
        await client.get_host_hints()

    assert len(ubus.requests) == 3


async def test_denied_after_fresh_login_raises_permission_error_without_looping(
    client: UbusClient, ubus: FakeUbus
) -> None:
    """A genuine ACL denial (captured) is retried exactly once, then raised."""
    ubus.respond(body("acl_login"))
    ubus.respond(body("acl_denied_object"))
    ubus.respond(login_body(TOKEN_2))
    ubus.respond(body("acl_denied_object"))

    with pytest.raises(UbusPermissionError) as excinfo:
        await client.call("luci", "setLocaltime", localtime=0)

    assert excinfo.value.rpc_code == JsonRpcError.ACCESS_DENIED
    assert len(ubus.requests) == 4


async def test_procedure_permission_denied_is_not_retried(
    client: UbusClient, ubus: FakeUbus
) -> None:
    """Status 6 from the procedure (captured file.read of /etc/shadow)."""
    ubus.respond(body("acl_login"))
    ubus.respond(body("acl_file_read_denied_path"))

    with pytest.raises(UbusPermissionError) as excinfo:
        await client.file_read("/etc/shadow")

    assert excinfo.value.status == UbusStatus.PERMISSION_DENIED
    assert len(ubus.requests) == 2


async def test_concurrent_rejections_share_one_relogin(client: UbusClient, ubus: FakeUbus) -> None:
    def route(request: dict[str, Any]) -> str:
        sid = request["params"][0]
        if sid == NULL_SESSION_ID:
            logins = sum(1 for r in ubus.requests if r["params"][0] == NULL_SESSION_ID)
            return login_body(TOKEN_1 if logins == 1 else TOKEN_2)
        if sid == TOKEN_1:
            return body("call_expired_session")
        return body("system_board")

    ubus.route = route
    await client.login()
    boards = await asyncio.gather(*(client.get_system_board() for _ in range(5)))

    assert {b.release.version for b in boards} == {"25.12.5"}
    logins = [r for r in ubus.requests if r["params"][0] == NULL_SESSION_ID]
    assert len(logins) == 2


async def test_proactive_renewal_when_session_lapsed(
    client: UbusClient, ubus: FakeUbus, monkeypatch: pytest.MonkeyPatch
) -> None:
    ubus.respond(login_body(TOKEN_1))
    ubus.respond(login_body(TOKEN_2))
    ubus.respond(body("system_board"))

    await client.login()
    clock = __import__("time").monotonic() + 1000
    monkeypatch.setattr("aioubus.client.time.monotonic", lambda: clock)

    await client.get_system_board()

    assert [r["params"][0] for r in ubus.requests] == [
        NULL_SESSION_ID,
        NULL_SESSION_ID,
        TOKEN_2,
    ]


async def test_successful_call_extends_local_expiry(
    client: UbusClient, ubus: FakeUbus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rpcd resets the idle timer on each authorized call; mirror that."""
    now = [1000.0]
    monkeypatch.setattr("aioubus.client.time.monotonic", lambda: now[0])
    ubus.respond(login_body())
    ubus.respond(body("system_board"), repeat=True)

    await client.login()  # expires == 299
    for _ in range(5):
        now[0] += 200
        await client.get_system_board()

    logins = [r for r in ubus.requests if r["params"][0] == NULL_SESSION_ID]
    assert len(logins) == 1


async def test_renew_session_forces_new_login(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(login_body(TOKEN_1))
    ubus.respond(login_body(TOKEN_2))
    ubus.respond(result(0))

    await client.login()
    await client.renew_session()
    await client.call("uci", "revert", config="dhcp")

    assert ubus.requests[-1]["params"][0] == TOKEN_2
