"""Payload-shape robustness.

Every parser is fed systematically corrupted versions of real captured
payloads: each nested value is replaced in turn by values of the wrong type,
and each key is deleted. Parsing must either succeed or raise
``UbusResponseError`` -- never ``TypeError``, ``KeyError``, ``AttributeError``
or ``ValueError``. The one exception is a missing payload, which ``uci get``
really returns for a missing section or option (``UbusNotFoundError``).
"""

import contextlib
import copy
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from conftest import body, derived

from aioubus import UbusNotFoundError, UbusResponseError, normalize_mac
from aioubus.models import (
    BoardJson,
    HostapdClients,
    NetworkDevice,
    SessionInfo,
    SystemBoard,
    WirelessRadio,
    parse_dhcp_leases,
    parse_host_hints,
    parse_odhcpd_leases,
    parse_uci_config,
    parse_uci_section,
    parse_uci_value,
)

type Parser = Callable[[Any], object]

WRONG_VALUES: list[Any] = [None, True, 0, -1, "x", "", [], {}, [1], {"a": 1}, 1.5]


def payload(raw: str) -> Any:
    return json.loads(raw)["result"][1]


def _dict_each(fn: Callable[[str, Any], object]) -> Parser:
    def parse(data: Any) -> object:
        if not isinstance(data, dict):
            raise UbusResponseError("not a dict")
        return [fn(k, v) for k, v in data.items()]

    return parse


CASES: list[tuple[str, Parser, Any]] = [
    ("host_hints", parse_host_hints, payload(body("luci_getHostHints"))),
    ("dhcp_leases", parse_dhcp_leases, payload(body("luci_getDHCPLeases"))),
    ("dhcp_leases_v6", parse_dhcp_leases, payload(derived("luci_getDHCPLeases_infinite_and_v6"))),
    (
        "odhcpd",
        parse_odhcpd_leases,
        payload(body("dhcp_ipv4leases", "openwrt-25.12.5-odhcpd")),
    ),
    ("session", SessionInfo.from_json, payload(body("acl_login"))),
    ("system_board", SystemBoard.from_json, payload(body("system_board"))),
    ("board_json", BoardJson.from_json, payload(body("luci_getBoardJSON"))),
    (
        "network_devices",
        _dict_each(NetworkDevice.from_json),
        payload(body("luci_getNetworkDevices")),
    ),
    (
        "hostapd",
        lambda p: HostapdClients.from_json("phy0-ap0", p),
        payload(derived("hostapd_get_clients_wireless")),
    ),
    (
        "wireless",
        _dict_each(WirelessRadio.from_json),
        payload(body("luci_getWirelessDevices", "openwrt-25.12.5-hwsim")),
    ),
    (
        "hostapd_hwsim",
        lambda p: HostapdClients.from_json("phy0-ap0", p),
        payload(body("hostapd_get_clients_phy0-ap0", "openwrt-25.12.5-hwsim")),
    ),
    ("uci_config", lambda p: parse_uci_config(p, "t"), payload(body("uci_get_dhcp_type_dnsmasq"))),
    ("uci_section", lambda p: parse_uci_section(p, "t"), payload(body("uci_get_section"))),
    ("uci_value", lambda p: parse_uci_value(p, "t"), payload(body("uci_get_list_option"))),
]


def _paths(node: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    yield prefix
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _paths(value, (*prefix, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _paths(value, (*prefix, index))


def _replace(root: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return value
    clone = copy.deepcopy(root)
    node = clone
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return clone


def _delete(root: Any, path: tuple[Any, ...]) -> Any:
    clone = copy.deepcopy(root)
    node = clone
    for step in path[:-1]:
        node = node[step]
    del node[path[-1]]
    return clone


def _mutations(data: Any) -> Iterator[Any]:
    for path in _paths(data):
        for value in WRONG_VALUES:
            yield _replace(data, path, value)
        if path:
            yield _delete(data, path)


@pytest.mark.parametrize(("name", "parse", "data"), CASES, ids=[c[0] for c in CASES])
def test_real_payload_parses(name: str, parse: Parser, data: Any) -> None:
    parse(data)


@pytest.mark.parametrize(("name", "parse", "data"), CASES, ids=[c[0] for c in CASES])
def test_corrupted_payloads_only_raise_response_error(name: str, parse: Parser, data: Any) -> None:
    count = 0
    for mutated in _mutations(data):
        count += 1
        allowed: tuple[type[Exception], ...] = (UbusResponseError,)
        if mutated is None:
            allowed += (UbusNotFoundError,)
        with contextlib.suppress(*allowed):
            parse(mutated)
    assert count > 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        ("aabbccddeeff", "aa:bb:cc:dd:ee:ff"),
        ("AABB.CCDD.EEFF", "aa:bb:cc:dd:ee:ff"),
        (" 02:42:C0:A8:4D:11 ", "02:42:c0:a8:4d:11"),
    ],
)
def test_normalize_mac(raw: str, expected: str) -> None:
    assert normalize_mac(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:ff:00", "gg:bb:cc:dd:ee:ff", "*"]
)
def test_normalize_mac_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="MAC"):
        normalize_mac(raw)
