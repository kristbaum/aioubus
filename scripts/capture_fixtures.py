"""Capture raw ubus JSON-RPC responses from a live OpenWrt device.

Writes one JSON file per scenario to ``OUT_DIR``::

    {"source": ..., "request": ..., "http_status": ..., "body": "<raw body>"}

Session tokens are replaced with sequential placeholders and passwords are
redacted, so the files are safe to commit. Review ``system_board`` output
before committing: it contains the device's hostname and model.

Usage::

    AIOUBUS_PASSWORD=... python scripts/capture_fixtures.py \\
        http://192.168.1.1/ubus tests/fixtures/openwrt-<release> "<release>"

Optional: ``AIOUBUS_USERNAME`` (default ``root``), and ``AIOUBUS_ACL_USER`` /
``AIOUBUS_ACL_PASSWORD`` for a restricted account using the README's ACL.

The committed fixtures were captured from ``openwrt/rootfs`` Docker images
(``x86-64-25.12.5`` and ``x86-64-24.10.8``) with ``rpcd-mod-luci``,
``rpcd-mod-file`` and ``luci-base`` installed, real DHCP clients on a second
Docker network, and two extra ACL groups: the README's ``aioubus`` group for
the restricted user, and a capture-only group granting root
``luci-rpc.noSuchMethod``, ``file.read`` of ``/tmp/does-not-exist`` and
``uci.revert`` so those status codes are reachable. Without that group root
gets ``-32002`` for these calls: its login grants every ACL group, not every
procedure.

``openwrt-25.12.5-hwsim`` was captured from the QEMU lab with
``real_test/lab.sh capture``, which installs the capture-only group for the
duration of the capture.
"""

import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

NULL_SID = "0" * 32


class Capture:
    def __init__(self, url: str, out: Path, tag: str) -> None:
        self.url = url
        self.out = out
        self.tag = tag
        self.tokens: dict[str, str] = {}
        self.secrets: list[str] = []
        self.ctx = ssl.create_default_context()
        if os.environ.get("AIOUBUS_INSECURE"):
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        out.mkdir(parents=True, exist_ok=True)

    def post(self, data: bytes) -> tuple[int, str]:
        req = urllib.request.Request(
            self.url, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15, context=self.ctx) as resp:
            return int(resp.status), resp.read().decode()

    def scrub(self, text: str) -> str:
        for real, fake in self.tokens.items():
            text = text.replace(real, fake)
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def save(self, name: str, request: object, status: int, text: str) -> None:
        record = {
            "source": f"captured from OpenWrt {self.tag}",
            "request": json.loads(self.scrub(json.dumps(request))),
            "http_status": status,
            "body": self.scrub(text),
        }
        (self.out / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")

    def raw(self, name: str, request: dict[str, Any]) -> Any:
        status, text = self.post(json.dumps(request).encode())
        self.save(name, request, status, text)
        return json.loads(text)

    def rpc(self, name: str, method: str, params: list[Any]) -> Any:
        return self.raw(name, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    def login(
        self,
        name: str,
        user: str,
        password: str,
        *,
        timeout: int | None = None,
        secret: bool = True,
    ) -> str:
        if secret:
            self.secrets.append(password)
        args: dict[str, Any] = {"username": user, "password": password}
        if timeout is not None:
            args["timeout"] = timeout
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "call",
            "params": [NULL_SID, "session", "login", args],
        }
        status, text = self.post(json.dumps(request).encode())
        result = json.loads(text).get("result", [1])
        sid = ""
        if result[0] == 0:
            sid = result[1]["ubus_rpc_session"]
            self.tokens[sid] = f"{len(self.tokens) + 1:032x}"
        self.save(name, request, status, text)
        return sid


def main() -> None:
    url, out, tag = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    user = os.environ.get("AIOUBUS_USERNAME", "root")
    password = os.environ["AIOUBUS_PASSWORD"]
    cap = Capture(url, out, tag)

    root = cap.login("login_ok", user, password)
    if not root:
        sys.exit("login failed")
    cap.login("login_wrong_password", user, password + "-wrong")
    cap.login("login_unknown_user", "nobody-" + user, "not-a-password", secret=False)

    calls: dict[str, tuple[str, str, dict[str, Any]]] = {
        "luci_getHostHints": ("luci-rpc", "getHostHints", {}),
        "luci_getDHCPLeases": ("luci-rpc", "getDHCPLeases", {}),
        "luci_getDHCPLeases_family4": ("luci-rpc", "getDHCPLeases", {"family": 4}),
        "luci_getWirelessDevices": ("luci-rpc", "getWirelessDevices", {}),
        "luci_getNetworkDevices": ("luci-rpc", "getNetworkDevices", {}),
        "luci_getBoardJSON": ("luci-rpc", "getBoardJSON", {}),
        "system_board": ("system", "board", {}),
        "uci_get_dhcp_type_dnsmasq": ("uci", "get", {"config": "dhcp", "type": "dnsmasq"}),
        "uci_get_option": (
            "uci",
            "get",
            {"config": "dhcp", "section": "lan", "option": "interface"},
        ),
        "uci_get_list_option": (
            "uci",
            "get",
            {"config": "dhcp", "section": "lan", "option": "ra_flags"},
        ),
        "uci_get_section": ("uci", "get", {"config": "dhcp", "section": "lan"}),
        "uci_get_missing_config": ("uci", "get", {"config": "doesnotexist"}),
        "uci_get_missing_section": ("uci", "get", {"config": "dhcp", "section": "doesnotexist"}),
        "uci_get_missing_option": (
            "uci",
            "get",
            {"config": "dhcp", "section": "lan", "option": "doesnotexist"},
        ),
        "file_read_leases": ("file", "read", {"path": "/tmp/dhcp.leases"}),  # noqa: S108
        "file_read_missing": ("file", "read", {"path": "/tmp/does-not-exist"}),  # noqa: S108
        "dhcp_ipv4leases": ("dhcp", "ipv4leases", {}),
        "method_not_found": ("luci-rpc", "noSuchMethod", {}),
        "object_not_found": ("nosuchobject", "x", {}),
        "invalid_argument": ("luci-rpc", "getDHCPLeases", {"family": 5}),
    }
    for name, (obj, proc, args) in calls.items():
        cap.rpc(name, "call", [root, obj, proc, args])

    # hostapd: one capture per radio interface, if any.
    names = cap.raw("list_all", {"jsonrpc": "2.0", "id": 1, "method": "list"})["result"]
    for obj in names:
        if obj.startswith("hostapd."):
            iface = obj.removeprefix("hostapd.")
            cap.rpc(f"hostapd_get_clients_{iface}", "call", [root, obj, "get_clients", {}])
    cap.rpc("list_empty_params", "list", [])
    cap.rpc("list_pattern_hostapd", "list", ["hostapd.*"])
    cap.rpc("list_pattern_luci_rpc", "list", ["luci-rpc"])
    cap.rpc("list_pattern_multi", "list", ["system", "file"])
    cap.rpc("call_bogus_session", "call", ["f" * 32, "luci-rpc", "getHostHints", {}])
    cap.rpc("call_null_session", "call", [NULL_SID, "luci-rpc", "getHostHints", {}])
    cap.rpc("call_no_payload_uci_revert", "call", [root, "uci", "revert", {"config": "dhcp"}])

    short = cap.login("login_short_timeout", user, password, timeout=1)
    time.sleep(3)
    cap.rpc("call_expired_session", "call", [short, "luci-rpc", "getHostHints", {}])

    acl_user = os.environ.get("AIOUBUS_ACL_USER")
    acl_password = os.environ.get("AIOUBUS_ACL_PASSWORD")
    if acl_user and acl_password:
        hass = cap.login("acl_login", acl_user, acl_password)
        for name, params in {
            "acl_getHostHints": ["luci-rpc", "getHostHints", {}],
            "acl_file_read_allowed": ["file", "read", {"path": "/tmp/dhcp.leases"}],  # noqa: S108
            "acl_file_read_denied_path": ["file", "read", {"path": "/etc/shadow"}],
            "acl_uci_get_allowed": ["uci", "get", {"config": "dhcp", "type": "dnsmasq"}],
            "acl_uci_get_denied_config": ["uci", "get", {"config": "rpcd"}],
            "acl_denied_object": ["luci", "setLocaltime", {"localtime": 0}],
            "acl_dhcp_ipv4leases_absent": ["dhcp", "ipv4leases", {}],
            "acl_system_board": ["system", "board", {}],
        }.items():
            cap.rpc(name, "call", [hass, *params])

    for name, data in {
        "malformed_request_json": b"{not json",
        "request_wrong_jsonrpc_version": json.dumps(
            {"jsonrpc": "1.0", "id": 1, "method": "call", "params": []}
        ).encode(),
        "unknown_rpc_method": json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "subscribe", "params": []}
        ).encode(),
    }.items():
        status, text = cap.post(data)
        cap.save(name, {"raw_body": data.decode()}, status, text)

    print(f"captured {len(list(out.glob('*.json')))} files into {out}")


if __name__ == "__main__":
    main()
