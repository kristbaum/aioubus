"""Tests against a real OpenWrt device. Skipped unless configured.

    AIOUBUS_LIVE_URL=http://192.168.1.1/ubus \\
    AIOUBUS_LIVE_PASSWORD=... \\
    [AIOUBUS_LIVE_USERNAME=root] \\
    [AIOUBUS_LIVE_ACL_USER=hass AIOUBUS_LIVE_ACL_PASSWORD=...] \\
    [AIOUBUS_LIVE_EXPECT_WIRED=mac=name,... AIOUBUS_LIVE_EXPECT_WIFI=mac=name,...] \\
    pytest -m live

The ``EXPECT`` variables list hosts that must be visible on the router; the
QEMU lab in ``real_test/`` sets them from its fake devices (``lab.sh test``).

HTTPS URLs are tested with certificate verification disabled.
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

import pytest
from yarl import URL

from aioubus import (
    HostapdClient,
    UbusClient,
    UbusNotFoundError,
    UbusPermissionError,
)

LIVE_URL = os.environ.get("AIOUBUS_LIVE_URL")


def expected_hosts(var: str) -> dict[str, str]:
    """Parse ``mac=name,mac=name`` from an environment variable."""
    pairs = (item.partition("=") for item in os.environ.get(var, "").split(",") if item)
    return {mac.lower(): name for mac, _, name in pairs}


EXPECT_WIRED = expected_hosts("AIOUBUS_LIVE_EXPECT_WIRED")
EXPECT_WIFI = expected_hosts("AIOUBUS_LIVE_EXPECT_WIFI")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE_URL, reason="AIOUBUS_LIVE_URL not set"),
]


def make_client(username: str, password: str, *, session_timeout: int | None = None) -> UbusClient:
    assert LIVE_URL is not None
    url = URL(LIVE_URL)
    assert url.host is not None
    assert url.scheme in {"http", "https"}, LIVE_URL
    return UbusClient(
        url.host,
        username,
        password,
        scheme="https" if url.scheme == "https" else "http",
        port=url.explicit_port,
        path=url.path,
        verify_ssl=False,
        session_timeout=session_timeout,
    )


@pytest.fixture
async def live() -> AsyncIterator[UbusClient]:
    client = make_client(
        os.environ.get("AIOUBUS_LIVE_USERNAME", "root"), os.environ["AIOUBUS_LIVE_PASSWORD"]
    )
    async with client:
        yield client


async def test_live_read_everything(live: UbusClient) -> None:
    info = await live.login()
    assert info.timeout > 0

    hints = await live.get_host_hints()
    assert hints
    assert all(mac == mac.lower() for mac in hints)

    await live.get_dhcp_leases()
    board = await live.get_system_board()
    assert board.release.distribution == "OpenWrt"
    await live.get_board_json()
    assert await live.get_network_devices()
    sections = await live.uci_get_config("dhcp")
    assert sections
    await live.list_hostapd_interfaces()
    assert "session" in await live.list_object_names()
    assert "luci-rpc" in await live.list_objects("luci-rpc")


async def test_live_uci_missing(live: UbusClient) -> None:
    with pytest.raises(UbusNotFoundError):
        await live.uci_get_section("dhcp", "doesnotexist")
    with pytest.raises(UbusNotFoundError):
        await live.uci_get_option("dhcp", "lan", "doesnotexist")
    # uci ACLs are per config name, and no ACL names a config that does not exist.
    with pytest.raises(UbusPermissionError):
        await live.uci_get_config("doesnotexist")


async def test_live_dnsmasq_or_odhcpd(live: UbusClient) -> None:
    try:
        await live.get_dnsmasq_leases()
    except UbusNotFoundError:
        pytest.skip(reason="no dnsmasq lease file")
    with contextlib.suppress(UbusNotFoundError):  # odhcpd-ipv6only
        await live.get_odhcpd_ipv4_leases()


async def test_live_wireless_or_not_found(live: UbusClient) -> None:
    try:
        radios = await live.get_wireless_devices()
    except UbusNotFoundError:
        return
    for iface in await live.list_hostapd_interfaces():
        await live.get_hostapd_clients(iface)
    assert radios is not None


async def test_live_session_expiry_relogin() -> None:
    client = make_client(
        os.environ.get("AIOUBUS_LIVE_USERNAME", "root"),
        os.environ["AIOUBUS_LIVE_PASSWORD"],
        session_timeout=2,
    )
    async with client:
        await client.get_system_board()
        generation = client._token_generation
        # Let the session expire on the device, while defeating the client's
        # proactive renewal so the server-side rejection path is exercised.
        await asyncio.sleep(3)
        client._expires_at = float("inf")
        await client.get_system_board()
        assert client._token_generation == generation + 1


@pytest.mark.skipif(not EXPECT_WIRED, reason="AIOUBUS_LIVE_EXPECT_WIRED not set")
async def test_live_wired_hosts_visible(live: UbusClient) -> None:
    hints = await live.get_host_hints()
    assert EXPECT_WIRED.keys() <= hints.keys()
    for mac, name in EXPECT_WIRED.items():
        assert hints[mac].ipv4_addresses, mac
        # Hint names come from reverse DNS, so dnsmasq appends the local
        # domain ("nas.lan"); the lease file below has the bare DHCP hostname.
        assert (hints[mac].name or "").partition(".")[0] == name

    leases = {lease.mac: lease for lease in await live.get_dnsmasq_leases()}
    assert EXPECT_WIRED.keys() <= leases.keys()
    for mac, name in EXPECT_WIRED.items():
        assert leases[mac].hostname == name


@pytest.mark.skipif(not EXPECT_WIFI, reason="AIOUBUS_LIVE_EXPECT_WIFI not set")
async def test_live_wifi_stations_associated(live: UbusClient) -> None:
    interfaces = await live.list_hostapd_interfaces()
    assert interfaces
    stations: dict[str, HostapdClient] = {}
    for iface in interfaces:
        result = await live.get_hostapd_clients(iface)
        assert result.frequency == 2412  # lab AP: 2.4 GHz, channel 1
        stations.update(result.clients)
    assert EXPECT_WIFI.keys() <= stations.keys()
    for mac in EXPECT_WIFI:
        assert stations[mac].authorized
        assert stations[mac].associated

    radios = await live.get_wireless_devices()
    assert radios


@pytest.mark.skipif(not os.environ.get("AIOUBUS_LIVE_ACL_USER"), reason="no ACL user")
async def test_live_restricted_acl() -> None:
    client = make_client(
        os.environ["AIOUBUS_LIVE_ACL_USER"], os.environ["AIOUBUS_LIVE_ACL_PASSWORD"]
    )
    async with client:
        assert await client.get_host_hints()
        await client.get_system_board()
        await client.get_dnsmasq_leases()
        with pytest.raises(UbusPermissionError):
            await client.file_read("/etc/shadow")
        with pytest.raises(UbusPermissionError):
            await client.call("luci", "setLocaltime", localtime=0)
