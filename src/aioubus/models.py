"""Typed models for ubus responses.

Each model is a frozen dataclass built by a ``from_json``-style parser that
validates shape and raises :class:`UbusResponseError` on mismatch. MAC
addresses are normalized with :func:`normalize_mac` (lowercase, colon
separated). Models whose upstream schema is open-ended or could not be
verified against a live device also carry the untouched payload as ``raw``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Self

from ._parse import (
    JsonObject,
    as_list,
    as_object,
    get_int,
    get_str,
    normalize_mac,
    opt_bool,
    opt_int,
    opt_str,
    parse_mac,
    str_tuple,
)
from .exceptions import UbusNotFoundError, UbusResponseError

_EMPTY: Mapping[str, Any] = MappingProxyType({})

type AddressFamily = Literal[4, 6]
IPV4: AddressFamily = 4
IPV6: AddressFamily = 6

# odhcpd encodes an infinite lease as (uint32_t)-1; blobmsg-json prints int32
# values signed, so it arrives as -1. Accept the unsigned form too.
_INFINITE_U32 = (-1, 0xFFFFFFFF)


def _frozen(obj: JsonObject) -> Mapping[str, Any]:
    return MappingProxyType(obj)


def _payload(payload: object, where: str) -> JsonObject:
    """Validate a top-level payload; ``None`` (no data) reads as empty."""
    return {} if payload is None else as_object(payload, where)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Result of ``session.login``.

    The session token itself is deliberately not exposed.
    """

    timeout: int
    """Idle timeout in seconds; every authorized call resets it."""
    expires: int
    """Seconds until expiry at the time of login."""
    acls: Mapping[str, Mapping[str, tuple[str, ...]]]
    """Granted ACLs by scope (``ubus``, ``uci``, ``file``, ``access-group``)."""

    @classmethod
    def from_json(cls, data: object) -> Self:
        where = "session.login"
        data = _payload(data, where)
        acls: dict[str, Mapping[str, tuple[str, ...]]] = {}
        for scope, entries in as_object(data.get("acls", {}), f"{where}.acls").items():
            scope_obj = as_object(entries, f"{where}.acls.{scope}")
            acls[scope] = MappingProxyType(
                {name: str_tuple(scope_obj, name, f"{where}.acls.{scope}") for name in scope_obj}
            )
        return cls(
            timeout=get_int(data, "timeout", where),
            expires=get_int(data, "expires", where),
            acls=MappingProxyType(acls),
        )


@dataclass(frozen=True, slots=True)
class HostHint:
    """One entry of ``luci-rpc getHostHints``.

    Host hints merge the kernel neighbour table, DHCP leases, ``/etc/ethers``,
    static leases from UCI, the router's own interfaces and reverse DNS.
    They do not distinguish wired from wireless hosts, and a present entry
    does not imply the host is currently online.
    """

    mac: str
    ipv4_addresses: tuple[str, ...]
    ipv6_addresses: tuple[str, ...]
    name: str | None
    """Hostname, or ``None`` when no source supplied one."""

    @classmethod
    def from_json(cls, mac: str, data: object) -> Self:
        where = f"getHostHints[{mac}]"
        obj = as_object(data, where)
        return cls(
            mac=parse_mac(mac, where),
            ipv4_addresses=str_tuple(obj, "ipaddrs", where),
            ipv6_addresses=str_tuple(obj, "ip6addrs", where),
            name=opt_str(obj, "name", where),
        )


def parse_host_hints(payload: object) -> dict[str, HostHint]:
    """Parse ``getHostHints`` into a dict keyed by normalized MAC."""
    hints = (
        HostHint.from_json(mac, data) for mac, data in _payload(payload, "getHostHints").items()
    )
    return {hint.mac: hint for hint in hints}


@dataclass(frozen=True, slots=True)
class DhcpLease:
    """One entry of ``luci-rpc getDHCPLeases`` (dnsmasq or odhcpd backed)."""

    family: AddressFamily
    ip_address: str
    mac: str | None
    hostname: str | None
    expires: int | None
    """Seconds remaining; 0 if already expired; ``None`` for infinite leases."""
    interface: str | None = None
    duid: str | None = None
    iaid: str | None = None
    ipv6_addresses: tuple[str, ...] = ()
    """For DHCPv6 leases: all assigned addresses/prefixes in CIDR form."""

    @classmethod
    def from_json(cls, data: object, family: AddressFamily, index: int) -> Self:
        where = f"getDHCPLeases.dhcp{'' if family == IPV4 else '6'}_leases[{index}]"
        obj = as_object(data, where)
        # rpcd-mod-luci emits ``"expires": false`` for infinite leases.
        raw_expires = obj.get("expires")
        expires = None if raw_expires is False else opt_int(obj, "expires", where)
        mac = opt_str(obj, "macaddr", where)
        return cls(
            family=family,
            ip_address=get_str(obj, "ipaddr" if family == IPV4 else "ip6addr", where),
            mac=parse_mac(mac, where) if mac is not None else None,
            hostname=opt_str(obj, "hostname", where),
            expires=expires,
            interface=opt_str(obj, "interface", where),
            duid=opt_str(obj, "duid", where),
            iaid=opt_str(obj, "iaid", where),
            ipv6_addresses=str_tuple(obj, "ip6addrs", where),
        )


def parse_dhcp_leases(payload: object) -> tuple[DhcpLease, ...]:
    """Parse ``getDHCPLeases``; IPv4 leases first, then IPv6."""
    payload = _payload(payload, "getDHCPLeases")
    leases: list[DhcpLease] = []
    sections: tuple[tuple[str, AddressFamily], ...] = (
        ("dhcp_leases", IPV4),
        ("dhcp6_leases", IPV6),
    )
    for key, family in sections:
        entries = as_list(payload.get(key, []), f"getDHCPLeases.{key}")
        leases.extend(DhcpLease.from_json(item, family, i) for i, item in enumerate(entries))
    return tuple(leases)


@dataclass(frozen=True, slots=True)
class OdhcpdLease:
    """One entry of odhcpd's ``dhcp ipv4leases``."""

    interface: str
    mac: str
    ip_address: str
    hostname: str | None
    """``None`` when odhcpd reported an empty hostname."""
    valid: int | None
    """Seconds remaining; ``None`` for infinite leases."""
    flags: frozenset[str]
    """e.g. ``bound``, ``static``, ``broken-hostname``."""
    accept_reconfigure: bool | None = None
    duid: str | None = None
    iaid: int | None = None

    @classmethod
    def from_json(cls, interface: str, data: object, index: int) -> Self:
        where = f"ipv4leases.device[{interface}].leases[{index}]"
        obj = as_object(data, where)
        valid = opt_int(obj, "valid", where)
        accept = opt_bool(obj, "accept-reconf", where)
        if accept is None:
            # Name used by odhcpd releases before 2024.
            accept = opt_bool(obj, "accept-reconf-nonce", where)
        return cls(
            interface=interface,
            mac=parse_mac(obj.get("mac"), where),
            ip_address=get_str(obj, "address", where),
            hostname=opt_str(obj, "hostname", where) or None,
            valid=None if valid in _INFINITE_U32 else valid,
            flags=frozenset(str_tuple(obj, "flags", where)),
            accept_reconfigure=accept,
            duid=opt_str(obj, "duid", where),
            iaid=opt_int(obj, "iaid", where),
        )


def parse_odhcpd_leases(payload: object) -> tuple[OdhcpdLease, ...]:
    devices = as_object(_payload(payload, "ipv4leases").get("device", {}), "ipv4leases.device")
    leases: list[OdhcpdLease] = []
    for interface, device in devices.items():
        dev = as_object(device, f"ipv4leases.device[{interface}]")
        entries = as_list(dev.get("leases", []), f"ipv4leases.device[{interface}].leases")
        leases.extend(OdhcpdLease.from_json(interface, item, i) for i, item in enumerate(entries))
    return tuple(leases)


@dataclass(frozen=True, slots=True)
class DnsmasqLease:
    """One IPv4 line of a dnsmasq lease file (read via ``file.read``)."""

    expires_at: int | None
    """Absolute expiry as a Unix timestamp; ``None`` for infinite leases."""
    mac: str | None
    """``None`` if the hardware address is not a 48-bit MAC."""
    hwaddr: str
    """Hardware address exactly as written by dnsmasq."""
    ip_address: str
    hostname: str | None
    client_id: str | None


_DNSMASQ_MIN_FIELDS = 4
_DNSMASQ_CLIENT_ID = 4


def parse_dnsmasq_leases(text: str) -> tuple[DnsmasqLease, ...]:
    """Parse the IPv4 entries of a dnsmasq lease file.

    Format per line: ``<expiry> <hwaddr> <ip> <hostname|*> <client-id|*>``.
    IPv6 entries (after the ``duid`` line) are skipped. Malformed lines raise
    :class:`UbusResponseError`.
    """
    leases: list[DnsmasqLease] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "duid":
            break
        if len(fields) < _DNSMASQ_MIN_FIELDS:
            raise UbusResponseError(f"dnsmasq lease line {lineno}: expected at least 4 fields")
        try:
            expiry = int(fields[0])
        except ValueError:
            raise UbusResponseError(f"dnsmasq lease line {lineno}: invalid expiry") from None
        try:
            mac: str | None = normalize_mac(fields[1])
        except ValueError:
            mac = None
        client_id = fields[_DNSMASQ_CLIENT_ID] if len(fields) > _DNSMASQ_CLIENT_ID else "*"
        leases.append(
            DnsmasqLease(
                expires_at=expiry or None,
                mac=mac,
                hwaddr=fields[1],
                ip_address=fields[2],
                hostname=None if fields[3] == "*" else fields[3],
                client_id=None if client_id == "*" else client_id,
            )
        )
    return tuple(leases)


@dataclass(frozen=True, slots=True)
class HostapdClient:
    """One station from ``hostapd.<iface> get_clients``.

    Field set derived from hostapd's ``ubus.c`` in openwrt/openwrt; driver
    statistics (``signal``, byte/packet counters, rates) are only present
    when the driver reports them. For 802.1X wired objects only
    ``authorized`` is reported. Verified against hostapd on a virtual
    (``mac80211_hwsim``) 2.4 GHz radio; not against real Wi-Fi hardware.
    """

    mac: str
    authorized: bool
    authenticated: bool | None = None
    associated: bool | None = None
    preauth: bool | None = None
    wds: bool | None = None
    wmm: bool | None = None
    ht: bool | None = None
    vht: bool | None = None
    he: bool | None = None
    wps: bool | None = None
    mfp: bool | None = None
    aid: int | None = None
    signal: int | None = None
    """Signal strength in dBm."""
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None
    rx_rate: int | None = None
    """Current receive rate in kbit/s."""
    tx_rate: int | None = None
    """Current transmit rate in kbit/s."""
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_json(cls, mac: str, data: object) -> Self:
        where = f"get_clients.clients[{mac}]"
        obj = as_object(data, where)

        def pair(key: str) -> tuple[int | None, int | None]:
            sub = obj.get(key)
            if sub is None:
                return None, None
            sub_obj = as_object(sub, f"{where}.{key}")
            return opt_int(sub_obj, "rx", where), opt_int(sub_obj, "tx", where)

        rx_bytes, tx_bytes = pair("bytes")
        rx_packets, tx_packets = pair("packets")
        rx_rate, tx_rate = pair("rate")
        return cls(
            mac=parse_mac(mac, where),
            authorized=opt_bool(obj, "authorized", where) or False,
            authenticated=opt_bool(obj, "auth", where),
            associated=opt_bool(obj, "assoc", where),
            preauth=opt_bool(obj, "preauth", where),
            wds=opt_bool(obj, "wds", where),
            wmm=opt_bool(obj, "wmm", where),
            ht=opt_bool(obj, "ht", where),
            vht=opt_bool(obj, "vht", where),
            he=opt_bool(obj, "he", where),
            wps=opt_bool(obj, "wps", where),
            mfp=opt_bool(obj, "mfp", where),
            aid=opt_int(obj, "aid", where),
            signal=opt_int(obj, "signal", where),
            rx_bytes=rx_bytes,
            tx_bytes=tx_bytes,
            rx_packets=rx_packets,
            tx_packets=tx_packets,
            rx_rate=rx_rate,
            tx_rate=tx_rate,
            raw=_frozen(obj),
        )


@dataclass(frozen=True, slots=True)
class HostapdClients:
    """Result of ``hostapd.<iface> get_clients``."""

    interface: str
    frequency: int | None
    """Operating frequency in MHz; absent for wired 802.1X objects."""
    clients: Mapping[str, HostapdClient]
    """Stations keyed by normalized MAC."""

    @classmethod
    def from_json(cls, interface: str, payload: object) -> Self:
        payload = _payload(payload, f"hostapd.{interface}")
        clients = as_object(payload.get("clients", {}), f"hostapd.{interface}.clients")
        parsed = (HostapdClient.from_json(mac, data) for mac, data in clients.items())
        return cls(
            interface=interface,
            frequency=opt_int(payload, "freq", f"hostapd.{interface}"),
            clients=MappingProxyType({client.mac: client for client in parsed}),
        )


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """``release`` table of ``system board`` (from ``/usr/lib/os-release``)."""

    distribution: str | None
    version: str | None
    revision: str | None
    target: str | None
    description: str | None
    codename: str | None = None
    builddate: str | None = None
    firmware_url: str | None = None


@dataclass(frozen=True, slots=True)
class SystemBoard:
    """Result of procd's ``system board``: model and firmware version."""

    hostname: str | None
    model: str | None
    board_name: str | None
    kernel: str | None
    system: str | None
    rootfs_type: str | None
    release: ReleaseInfo
    raw: Mapping[str, Any] = field(default=_EMPTY, repr=False, compare=False)

    @classmethod
    def from_json(cls, payload: object) -> Self:
        where = "system.board"
        obj = _payload(payload, where)
        rel = as_object(obj.get("release", {}), f"{where}.release")
        rwhere = f"{where}.release"
        return cls(
            hostname=opt_str(obj, "hostname", where),
            model=opt_str(obj, "model", where),
            board_name=opt_str(obj, "board_name", where),
            kernel=opt_str(obj, "kernel", where),
            system=opt_str(obj, "system", where),
            rootfs_type=opt_str(obj, "rootfs_type", where),
            release=ReleaseInfo(
                distribution=opt_str(rel, "distribution", rwhere),
                version=opt_str(rel, "version", rwhere),
                revision=opt_str(rel, "revision", rwhere),
                target=opt_str(rel, "target", rwhere),
                description=opt_str(rel, "description", rwhere),
                codename=opt_str(rel, "codename", rwhere),
                builddate=opt_str(rel, "builddate", rwhere),
                firmware_url=opt_str(rel, "firmware_url", rwhere),
            ),
            raw=_frozen(obj),
        )


@dataclass(frozen=True, slots=True)
class BoardJson:
    """Result of ``luci-rpc getBoardJSON`` (the contents of ``/etc/board.json``).

    ``board.json`` describes hardware (model, default network layout, LEDs,
    switches); it does not contain the firmware version — use
    :meth:`UbusClient.get_system_board` for that.
    """

    model_id: str | None
    model_name: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_json(cls, payload: object) -> Self:
        obj = _payload(payload, "getBoardJSON")
        model = as_object(obj.get("model", {}), "getBoardJSON.model")
        return cls(
            model_id=opt_str(model, "id", "getBoardJSON.model"),
            model_name=opt_str(model, "name", "getBoardJSON.model"),
            raw=_frozen(obj),
        )


@dataclass(frozen=True, slots=True)
class InterfaceAddress:
    address: str
    netmask: str | None
    broadcast: str | None = None
    remote: str | None = None


def _addresses(obj: Mapping[str, Any], key: str, where: str) -> tuple[InterfaceAddress, ...]:
    result = []
    for i, item in enumerate(as_list(obj.get(key, []), f"{where}.{key}")):
        entry = as_object(item, f"{where}.{key}[{i}]")
        result.append(
            InterfaceAddress(
                address=get_str(entry, "address", where),
                netmask=opt_str(entry, "netmask", where),
                broadcast=opt_str(entry, "broadcast", where),
                remote=opt_str(entry, "remote", where),
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class NetworkDevice:
    """One entry of ``luci-rpc getNetworkDevices``."""

    name: str
    mac: str | None
    up: bool | None
    wireless: bool | None
    bridge: bool
    devtype: str | None
    mtu: int | None
    master: str | None
    ports: tuple[str, ...]
    ipv4_addresses: tuple[InterfaceAddress, ...]
    ipv6_addresses: tuple[InterfaceAddress, ...]
    carrier: bool | None
    speed: int | None
    """Link speed in Mbit/s, if known."""
    stats: Mapping[str, int]
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_json(cls, name: str, data: object) -> Self:
        where = f"getNetworkDevices[{name}]"
        obj = as_object(data, where)
        link = as_object(obj.get("link", {}), f"{where}.link")
        stats = as_object(obj.get("stats", {}), f"{where}.stats")
        mac = opt_str(obj, "mac", where)
        try:
            norm_mac = normalize_mac(mac) if mac is not None else None
        except ValueError:
            # Non-Ethernet link layers (tunnels, loopback) report other formats.
            norm_mac = None
        return cls(
            name=opt_str(obj, "name", where) or name,
            mac=norm_mac,
            up=opt_bool(obj, "up", where),
            wireless=opt_bool(obj, "wireless", where),
            bridge=opt_bool(obj, "bridge", where) or False,
            devtype=opt_str(obj, "devtype", where),
            mtu=opt_int(obj, "mtu", where),
            master=opt_str(obj, "master", where),
            ports=str_tuple(obj, "ports", where),
            ipv4_addresses=_addresses(obj, "ipaddrs", where),
            ipv6_addresses=_addresses(obj, "ip6addrs", where),
            carrier=opt_bool(link, "carrier", f"{where}.link"),
            speed=opt_int(link, "speed", f"{where}.link"),
            stats=MappingProxyType(
                {k: v for k in stats if (v := opt_int(stats, k, f"{where}.stats")) is not None}
            ),
            raw=_frozen(obj),
        )


@dataclass(frozen=True, slots=True)
class WirelessInterface:
    """An interface of a radio in ``luci-rpc getWirelessDevices``.

    Shape derived from netifd's ``network.wireless status`` plus the
    ``iwinfo`` table added by rpcd-mod-luci. Verified on virtual
    (``mac80211_hwsim``) radios with AP and station interfaces.
    """

    section: str | None
    ifname: str | None
    ssid: str | None
    mode: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class WirelessRadio:
    """One radio of ``luci-rpc getWirelessDevices``."""

    name: str
    up: bool | None
    pending: bool | None
    disabled: bool | None
    interfaces: tuple[WirelessInterface, ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_json(cls, name: str, data: object) -> Self:
        where = f"getWirelessDevices[{name}]"
        obj = as_object(data, where)
        interfaces = []
        for i, item in enumerate(as_list(obj.get("interfaces", []), f"{where}.interfaces")):
            iwhere = f"{where}.interfaces[{i}]"
            iface = as_object(item, iwhere)
            config = as_object(iface.get("config", {}), f"{iwhere}.config")
            iwinfo = as_object(iface.get("iwinfo", {}), f"{iwhere}.iwinfo")
            interfaces.append(
                WirelessInterface(
                    section=opt_str(iface, "section", iwhere),
                    ifname=opt_str(iface, "ifname", iwhere),
                    ssid=opt_str(iwinfo, "ssid", iwhere) or opt_str(config, "ssid", iwhere),
                    mode=opt_str(iwinfo, "mode", iwhere) or opt_str(config, "mode", iwhere),
                    raw=_frozen(iface),
                )
            )
        return cls(
            name=name,
            up=opt_bool(obj, "up", where),
            pending=opt_bool(obj, "pending", where),
            disabled=opt_bool(obj, "disabled", where),
            interfaces=tuple(interfaces),
            raw=_frozen(obj),
        )


type UciValue = str | tuple[str, ...]


def _uci_value(value: object, where: str) -> UciValue:
    if isinstance(value, str):
        return value
    items = as_list(value, where)
    if not all(isinstance(item, str) for item in items):
        raise UbusResponseError(f"{where}: expected string or array of strings")
    return tuple(items)


@dataclass(frozen=True, slots=True)
class UciSection:
    """A UCI section from ``uci get``."""

    name: str
    type: str
    anonymous: bool
    index: int | None
    options: Mapping[str, UciValue]
    """Option values: ``str`` for options, ``tuple[str, ...]`` for lists."""

    @classmethod
    def from_json(cls, data: object, where: str) -> Self:
        obj = as_object(data, where)
        options = {
            key: _uci_value(value, f"{where}.{key}")
            for key, value in obj.items()
            if not key.startswith(".")
        }
        return cls(
            name=get_str(obj, ".name", where),
            type=get_str(obj, ".type", where),
            anonymous=opt_bool(obj, ".anonymous", where) or False,
            index=opt_int(obj, ".index", where),
            options=MappingProxyType(options),
        )


def parse_uci_value(payload: object, where: str) -> UciValue:
    # rpcd answers a missing section or option with status 0 and no payload.
    if payload is None:
        raise UbusNotFoundError(f"{where}: no such option")
    payload = _payload(payload, where)
    if "value" not in payload:
        raise UbusResponseError(f"{where}: missing 'value'")
    return _uci_value(payload["value"], f"{where}.value")


def parse_uci_section(payload: object, where: str) -> UciSection:
    if payload is None:
        raise UbusNotFoundError(f"{where}: no such section")
    payload = _payload(payload, where)
    if "values" not in payload:
        raise UbusResponseError(f"{where}: missing 'values'")
    return UciSection.from_json(payload["values"], f"{where}.values")


def parse_uci_config(payload: object, where: str) -> dict[str, UciSection]:
    """Parse a package dump into sections keyed by name, in file order."""
    values = as_object(_payload(payload, where).get("values", {}), f"{where}.values")
    sections = [
        UciSection.from_json(data, f"{where}.values[{name}]") for name, data in values.items()
    ]
    sections.sort(key=lambda s: s.index if s.index is not None else 0)
    return {section.name: section for section in sections}
