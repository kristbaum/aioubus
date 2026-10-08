"""Typed wrappers, parsed from captured (or source-derived) payloads."""

import base64

import pytest
from conftest import RELEASES, TOKEN_1, FakeUbus, body, derived, login_body, result

from aioubus import (
    HostHint,
    UbusClient,
    UbusResponseError,
    parse_dnsmasq_leases,
)

pytestmark = pytest.mark.usefixtures("logged_in")


@pytest.fixture
def logged_in(ubus: FakeUbus) -> None:
    ubus.respond(login_body())


def last_params(ubus: FakeUbus) -> list[object]:
    params: list[object] = ubus.requests[-1]["params"]
    return params


async def test_get_host_hints_mixed_hosts(client: UbusClient, ubus: FakeUbus) -> None:
    """Captured on 25.12.5 with three DHCP clients, a static lease and neighbours."""
    ubus.respond(body("luci_getHostHints"))

    hints = await client.get_host_hints()

    assert last_params(ubus) == [TOKEN_1, "luci-rpc", "getHostHints", {}]
    # Keys and .mac are normalized to lowercase colon form.
    assert all(mac == hint.mac and mac == mac.lower() for mac, hint in hints.items())
    # Static UCI lease for a host that is not on the network: no addresses.
    assert hints["00:11:32:de:ad:01"] == HostHint(
        mac="00:11:32:de:ad:01", ipv4_addresses=(), ipv6_addresses=(), name="nas"
    )
    # DHCP client: several IPv4 addresses (lease + neighbour), with name.
    anna = hints["02:42:c0:a8:4d:11"]
    assert "192.168.77.132" in anna.ipv4_addresses
    assert anna.name is not None
    # Neighbour-only entries (the docker gateway, the router's eth0): no name.
    unnamed = [hint for hint in hints.values() if hint.name is None]
    assert len(unnamed) == 2
    assert all(hint.ipv4_addresses for hint in unnamed)
    # The router's own LAN interface is included, with an IPv6 address.
    own = next(h for h in hints.values() if "192.168.77.1" in h.ipv4_addresses)
    assert own.ipv6_addresses == ("fe80::58a6:60ff:fe71:4641",)


@pytest.mark.parametrize("release", [*RELEASES, "openwrt-25.12.5-odhcpd"])
async def test_get_host_hints_across_releases(
    client: UbusClient, ubus: FakeUbus, release: str
) -> None:
    ubus.respond(body("luci_getHostHints", release))

    hints = await client.get_host_hints()

    assert hints
    assert all(isinstance(h.ipv4_addresses, tuple) for h in hints.values())


async def test_get_host_hints_empty(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(result(0, {}))
    assert await client.get_host_hints() == {}


async def test_get_dhcp_leases_captured(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("luci_getDHCPLeases"))

    leases = await client.get_dhcp_leases()

    assert len(leases) == 3
    by_mac = {lease.mac: lease for lease in leases}
    pi = by_mac["b8:27:eb:12:ab:cd"]
    assert pi.family == 4
    assert pi.hostname == "raspberrypi"
    assert pi.ip_address == "192.168.77.240"
    assert pi.expires is not None
    assert pi.expires > 40000
    # Client that sent no hostname option.
    assert by_mac["02:42:c0:a8:4d:12"].hostname is None


async def test_get_dhcp_leases_family_argument(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("luci_getDHCPLeases_family4"))

    await client.get_dhcp_leases(family=4)

    assert last_params(ubus)[3] == {"family": 4}


async def test_get_dhcp_leases_infinite_and_ipv6(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(derived("luci_getDHCPLeases_infinite_and_v6"))

    v4_static, v4_expired, v6 = await client.get_dhcp_leases()

    assert v4_static.expires is None  # "expires": false
    assert v4_expired.expires == 0
    assert v6.family == 6
    assert v6.ip_address == "fd2a:510b:6393::abc"
    assert v6.ipv6_addresses == ("fd2a:510b:6393::abc/128",)
    assert v6.duid == "0001000129d6c6f20242c0a84d11"
    assert v6.interface == "br-lan"


async def test_odhcpd_ipv4_leases_captured(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("dhcp_ipv4leases", "openwrt-25.12.5-odhcpd"))

    leases = await client.get_odhcpd_ipv4_leases()

    assert last_params(ubus)[1:3] == ["dhcp", "ipv4leases"]
    by_mac = {lease.mac: lease for lease in leases}
    assert set(by_mac) == {"b8:27:eb:12:ab:cd", "02:42:c0:a8:4d:11", "02:42:c0:a8:4d:12"}
    pi = by_mac["b8:27:eb:12:ab:cd"]
    assert pi.interface == "br-lan"
    assert pi.hostname == "raspberrypi"
    assert pi.flags == {"bound"}
    assert pi.accept_reconfigure is False
    nohost = by_mac["02:42:c0:a8:4d:12"]
    assert nohost.hostname is None
    assert "broken-hostname" in nohost.flags


async def test_odhcpd_ipv4_leases_infinite(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(derived("dhcp_ipv4leases_infinite"))

    (lease,) = await client.get_odhcpd_ipv4_leases()

    assert lease.valid is None
    assert lease.iaid == 1
    assert lease.flags == {"bound", "static"}


async def test_odhcpd_ipv4_leases_legacy_field_names(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(derived("dhcp_ipv4leases_legacy_2023"))

    (lease,) = await client.get_odhcpd_ipv4_leases()

    assert lease.accept_reconfigure is False
    assert lease.valid == 43000


async def test_odhcpd_ipv6only_has_no_ipv4leases(client: UbusClient, ubus: FakeUbus) -> None:
    """Captured: odhcpd-ipv6only (25.12 default) lacks the method."""
    from aioubus import UbusMethodNotFoundError  # noqa: PLC0415

    ubus.respond(body("acl_dhcp_ipv4leases_absent"))

    with pytest.raises(UbusMethodNotFoundError):
        await client.get_odhcpd_ipv4_leases()


async def test_get_dnsmasq_leases_resolves_leasefile_from_uci(
    client: UbusClient, ubus: FakeUbus
) -> None:
    ubus.respond(body("uci_get_dhcp_type_dnsmasq"))
    ubus.respond(body("file_read_leases"))

    leases = await client.get_dnsmasq_leases()

    requests = ubus.requests
    assert requests[1]["params"][1:] == ["uci", "get", {"config": "dhcp", "type": "dnsmasq"}]
    assert requests[2]["params"][1:] == ["file", "read", {"path": "/tmp/dhcp.leases"}]
    by_mac = {lease.mac: lease for lease in leases}
    assert set(by_mac) == {"b8:27:eb:12:ab:cd", "02:42:c0:a8:4d:11", "02:42:c0:a8:4d:12"}
    pi = by_mac["b8:27:eb:12:ab:cd"]
    assert pi.hostname == "raspberrypi"
    assert pi.ip_address == "192.168.77.240"
    assert pi.client_id == "01:b8:27:eb:12:ab:cd"
    assert pi.expires_at == 1791520918
    assert by_mac["02:42:c0:a8:4d:12"].hostname is None  # "*"


async def test_get_dnsmasq_leases_explicit_path(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("file_read_leases"))

    await client.get_dnsmasq_leases("/tmp/custom.leases")

    assert last_params(ubus)[3] == {"path": "/tmp/custom.leases"}


def test_parse_dnsmasq_leases_edge_cases() -> None:
    text = (
        "1700000000 aa:bb:cc:dd:ee:ff 10.0.0.2 host1 01:aa:bb:cc:dd:ee:ff\n"
        "\n"
        "1700000001 20-00-01-02-03-04-05-06-07-08-09 10.0.0.3 ib-host *\n"
        "0 dc:a6:32:01:02:03 10.0.0.4 printer *\n"
        "duid 00:01:00:01:2c:6b:1c:9e:52:54:00:12:34:56\n"
        "1700000002 305419896 fd00::2 host6 00:01:00:01\n"
    )
    first, second, third = parse_dnsmasq_leases(text)
    assert third.expires_at is None  # 0 = infinite
    assert third.hostname == "printer"
    assert first.mac == "aa:bb:cc:dd:ee:ff"
    assert first.client_id == "01:aa:bb:cc:dd:ee:ff"
    assert first.expires_at == 1700000000
    assert second.mac is None  # not a 48-bit MAC
    assert second.hwaddr == "20-00-01-02-03-04-05-06-07-08-09"


@pytest.mark.parametrize("text", ["abc aa:bb:cc:dd:ee:ff 1.2.3.4 h *\n", "1 aa:bb:cc:dd:ee:ff\n"])
def test_parse_dnsmasq_leases_malformed(text: str) -> None:
    with pytest.raises(UbusResponseError):
        parse_dnsmasq_leases(text)


async def test_hostapd_list_and_clients(client: UbusClient, ubus: FakeUbus) -> None:
    await client.login()
    ubus.respond(derived("list_names_with_hostapd"))
    ubus.respond(derived("hostapd_get_clients_wireless"))

    interfaces = await client.list_hostapd_interfaces()
    clients = await client.get_hostapd_clients("hostapd.phy0-ap0")

    # The global "hostapd" object is not an interface; wired objects are kept.
    assert interfaces == ("eth1", "phy0-ap0", "phy1-ap0")
    assert ubus.requests[1] == {"jsonrpc": "2.0", "id": 2, "method": "list"}
    assert last_params(ubus) == [TOKEN_1, "hostapd.phy0-ap0", "get_clients", {}]
    assert clients.interface == "phy0-ap0"
    assert clients.frequency == 5180
    good = clients.clients["a4:c3:f0:11:22:33"]
    assert good.authorized
    assert good.associated
    assert good.signal == -52
    assert (good.rx_bytes, good.tx_bytes) == (1823311, 9412233)
    assert (good.rx_rate, good.tx_rate) == (866700, 780000)
    assert good.raw["aid"] == 1
    # Unauthorized stations are reported, not filtered out.
    pending = clients.clients["6c:40:08:aa:bb:cc"]
    assert pending.authorized is False
    assert pending.signal is None


async def test_hostapd_list_nginx_shape(client: UbusClient, ubus: FakeUbus) -> None:
    await client.login()
    ubus.respond(derived("nginx_list_names"))
    assert await client.list_hostapd_interfaces() == ("phy0-ap0",)


async def test_hostapd_wired_and_empty(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(derived("hostapd_get_clients_wired"))
    ubus.respond(derived("hostapd_get_clients_empty"))

    wired = await client.get_hostapd_clients("eth1")
    empty = await client.get_hostapd_clients("phy1-ap0")

    assert wired.frequency is None
    assert wired.clients["00:1b:21:aa:bb:cc"].authorized
    assert wired.clients["00:1b:21:aa:bb:cc"].associated is None
    assert empty.clients == {}


# Captured from the QEMU lab (real_test/): one AP on a mac80211_hwsim radio and
# the five Wi-Fi fakes of fakedevs.sh associated to it as stations.
HWSIM = "openwrt-25.12.5-hwsim"
LAB_WIFI_FAKES = {
    "f0:18:98:b2:00:01",
    "02:5a:3c:b2:00:02",
    "02:00:00:b2:00:03",
    "24:0a:c4:b2:00:04",
    "18:b4:30:b2:00:05",
}


async def test_hostapd_captured(client: UbusClient, ubus: FakeUbus) -> None:
    await client.login()
    ubus.respond(body("list_all", HWSIM))
    ubus.respond(body("hostapd_get_clients_phy0-ap0", HWSIM))

    interfaces = await client.list_hostapd_interfaces()
    clients = await client.get_hostapd_clients("phy0-ap0")

    # "hostapd" and "hostapd-auth" are daemon objects, not interfaces.
    assert interfaces == ("phy0-ap0",)
    assert clients.frequency == 2412
    assert clients.clients.keys() == LAB_WIFI_FAKES
    for station in clients.clients.values():
        assert station.authorized
        assert station.authenticated
        assert station.associated
        assert station.signal == -20
        assert station.rx_bytes is not None
        assert station.rx_rate is not None
    # Newer hostapd adds keys that the model does not name; they stay in raw.
    assert clients.clients["f0:18:98:b2:00:01"].raw["mbo"] is False


async def test_list_objects_hostapd_captured(client: UbusClient, ubus: FakeUbus) -> None:
    await client.login()
    ubus.respond(body("list_pattern_hostapd", HWSIM))

    objects = await client.list_objects("hostapd.*")

    assert list(objects) == ["hostapd.phy0-ap0"]
    assert objects["hostapd.phy0-ap0"]["get_clients"] == {}
    assert objects["hostapd.phy0-ap0"]["del_client"]["addr"] == "string"


async def test_wireless_devices_captured(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("luci_getWirelessDevices", HWSIM))

    radios = await client.get_wireless_devices()

    assert list(radios) == [f"radio{i}" for i in range(8)]
    (ap,) = radios["radio0"].interfaces
    assert (ap.section, ap.ifname, ap.ssid, ap.mode) == (
        "default_radio0",
        "phy0-ap0",
        "testnet",
        "Master",
    )
    (station,) = radios["radio1"].interfaces
    assert (station.section, station.ifname, station.mode) == ("fake_iphone", "phy1-sta0", "Client")
    assert radios["radio7"].disabled is True
    assert radios["radio7"].interfaces == ()


async def test_host_hints_captured_lab(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("luci_getHostHints", HWSIM))

    hints = await client.get_host_hints()

    # DHCP clients are named through reverse DNS, so with the local domain.
    nas = hints["00:11:32:a1:00:01"]
    assert nas.name == "nas.lan"
    assert nas.ipv4_addresses
    # The Wi-Fi fakes are station interfaces on the router itself; host hints
    # include the router's own interfaces, without address or name.
    for mac in LAB_WIFI_FAKES:
        assert hints[mac].name is None
        assert hints[mac].ipv4_addresses == ()


@pytest.mark.parametrize("release", RELEASES)
async def test_uci_get_config_by_type(client: UbusClient, ubus: FakeUbus, release: str) -> None:
    ubus.respond(body("uci_get_dhcp_type_dnsmasq", release))

    sections = await client.uci_get_config("dhcp", section_type="dnsmasq")

    (section,) = sections.values()
    assert section.type == "dnsmasq"
    assert section.anonymous
    assert section.index == 0
    assert section.options["leasefile"] == "/tmp/dhcp.leases"
    assert ".name" not in section.options


async def test_uci_get_section_and_options(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("uci_get_section"))
    ubus.respond(body("uci_get_option"))
    ubus.respond(body("uci_get_list_option"))

    section = await client.uci_get_section("dhcp", "lan")
    option = await client.uci_get_option("dhcp", "lan", "interface")
    flags = await client.uci_get_option("dhcp", "lan", "ra_flags")

    assert section.name == "lan"
    assert not section.anonymous
    assert section.options["ra_flags"] == ("managed-config", "other-config")
    assert option == "lan"
    assert flags == ("managed-config", "other-config")
    assert last_params(ubus)[3] == {"config": "dhcp", "section": "lan", "option": "ra_flags"}


async def test_uci_get_option_missing_value(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(result(0, {}))
    with pytest.raises(UbusResponseError):
        await client.uci_get_option("dhcp", "lan", "nope")


async def test_file_read(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("acl_file_read_allowed"))

    text = await client.file_read("/tmp/dhcp.leases")

    assert text.startswith("1791520918 b8:27:eb:12:ab:cd 192.168.77.240 raspberrypi")
    assert text.endswith("\n")


async def test_file_read_bytes(client: UbusClient, ubus: FakeUbus) -> None:
    raw = bytes(range(256))
    ubus.respond(result(0, {"data": base64.b64encode(raw).decode()}))
    ubus.respond(result(0, {"data": "!!not base64!!"}))

    assert await client.file_read_bytes("/etc/blob") == raw
    assert last_params(ubus)[3] == {"path": "/etc/blob", "base64": True}
    with pytest.raises(UbusResponseError):
        await client.file_read_bytes("/etc/blob")


async def test_file_read_missing_data(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(result(0, {}))
    with pytest.raises(UbusResponseError):
        await client.file_read("/x")


@pytest.mark.parametrize("release", RELEASES)
async def test_system_board(client: UbusClient, ubus: FakeUbus, release: str) -> None:
    ubus.respond(body("system_board", release))

    board = await client.get_system_board()

    assert board.model == "Example Board X1"
    assert board.release.distribution == "OpenWrt"
    assert board.release.version == release.split("-")[1]
    assert board.release.target == "x86/64"
    assert board.release.revision is not None


async def test_board_json(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("luci_getBoardJSON"))

    board = await client.get_board_json()

    assert board.model_id == "example-board-x1"
    assert board.model_name == "Example Board X1"
    assert "network" in board.raw


async def test_network_devices(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("luci_getNetworkDevices"))

    devices = await client.get_network_devices()

    lan = devices["br-lan"]
    assert lan.bridge
    assert lan.ports == ("eth1",)
    assert lan.mac == "5a:a6:60:71:46:41"
    assert lan.ipv4_addresses[0].address == "192.168.77.1"
    assert lan.ipv4_addresses[0].netmask == "255.255.255.0"
    assert lan.stats["rx_bytes"] > 0
    assert devices["eth1"].master == "br-lan"
    assert devices["lo"].mac is None or devices["lo"].mac == "00:00:00:00:00:00"


async def test_wireless_devices(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(derived("luci_getWirelessDevices"))

    radios = await client.get_wireless_devices()

    radio = radios["radio0"]
    assert radio.up
    assert radio.disabled is False
    (iface,) = radio.interfaces
    assert iface.ifname == "phy0-ap0"
    assert iface.ssid == "OpenWrt"
    assert iface.mode == "Master"


async def test_list_objects(client: UbusClient, ubus: FakeUbus) -> None:
    await client.login()
    ubus.respond(body("list_pattern_multi"))
    ubus.respond(body("list_pattern_hostapd"))

    objects = await client.list_objects("system", "file")
    none = await client.list_objects("hostapd.*")

    assert objects["file"]["read"] == {
        "path": "string",
        "base64": "boolean",
        "ubus_rpc_session": "string",
    }
    assert "board" in objects["system"]
    assert none == {}
    assert ubus.requests[1]["params"] == ["system", "file"]


async def test_list_objects_nginx_shape_is_rejected(client: UbusClient, ubus: FakeUbus) -> None:
    await client.login()
    ubus.respond(derived("nginx_list_verbose"))
    with pytest.raises(UbusResponseError, match="list_object_names"):
        await client.list_objects("hostapd.*")


@pytest.mark.parametrize("release", RELEASES)
async def test_list_object_names(client: UbusClient, ubus: FakeUbus, release: str) -> None:
    await client.login()
    ubus.respond(body("list_all", release))

    names = await client.list_object_names()

    assert "luci-rpc" in names
    assert "session" in names
    assert "params" not in ubus.requests[-1]


async def test_generic_call(client: UbusClient, ubus: FakeUbus) -> None:
    ubus.respond(body("call_no_payload_uci_revert"))
    ubus.respond(result(0, {"x": 1}))

    assert await client.call("uci", "revert", {"config": "dhcp"}) is None
    payload = await client.call("svc", "fn", {"a": 1, "obj": "m"}, a=2, procedure="kw")

    assert payload == {"x": 1}
    assert last_params(ubus) == [TOKEN_1, "svc", "fn", {"a": 2, "obj": "m", "procedure": "kw"}]
