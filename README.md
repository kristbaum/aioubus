# aioubus

Async Python client for OpenWrt's **ubus HTTP/JSON-RPC API** — the `/ubus`
endpoint served by `uhttpd-mod-ubus` (or `nginx-mod-ubus`) that LuCI itself
uses. Built for use as the dependency of a Home Assistant integration:
native `asyncio`, an injectable `aiohttp.ClientSession`, typed models, a real
exception hierarchy, and no Home Assistant imports.

```bash
pip install aioubus
```

Requires Python 3.14+ (what Home Assistant currently supports), `aiohttp`
and `yarl`.

## Why

- **`luci-mod-rpc` is gone.** OpenWrt removed it from LuCI master in
  [`3476cb26b0b`](https://github.com/openwrt/luci/commit/3476cb26b0b)
  (2026-08-07). It survives in the `openwrt-22.03`…`openwrt-25.12` branches
  only, so anything built on it (e.g. `openwrt-luci-rpc`) has about one
  release left.
- **The existing ubus library is synchronous.**
- **What this adds:** `luci-rpc getHostHints` (all known hosts, wired and
  wireless), HTTPS with certificate verification, native async, and
  automatic recovery from expired sessions.

## Quick start

```python
import asyncio

from aioubus import UbusClient


async def main() -> None:
    async with UbusClient("192.168.1.1", "root", "password") as client:
        board = await client.get_system_board()
        print(board.model, board.release.version)
        for mac, host in (await client.get_host_hints()).items():
            print(mac, host.name, host.ipv4_addresses)


asyncio.run(main())
```

Every example below is a snippet for the asyncio REPL (`python -m asyncio`,
which allows top-level `await`), run after this setup:

```python
from aioubus import UbusClient

client = UbusClient("192.168.1.1", "root", "password")
```

## What the device needs

| Feature | ubus object | OpenWrt package | Installed by default with LuCI |
|---|---|---|---|
| HTTP endpoint `/ubus` | — | `uhttpd-mod-ubus` (or `nginx-mod-ubus`) | yes |
| Login, sessions | `session` | `rpcd` | yes |
| `get_host_hints`, `get_dhcp_leases`, `get_network_devices`, `get_wireless_devices`, `get_board_json` | `luci-rpc` | `rpcd-mod-luci` | yes (dependency of `luci-base`) |
| `file_read`, `get_dnsmasq_leases` | `file` | `rpcd-mod-file` | yes (dependency of `luci-base`) |
| `uci_get_*` | `uci` | `rpcd` | yes |
| `get_system_board` | `system` | `procd` | always |
| `get_hostapd_clients`, `list_hostapd_interfaces` | `hostapd.<iface>` | `hostapd`/`wpad` (ubus support is built in) | on devices with Wi-Fi |
| `get_odhcpd_ipv4_leases` | `dhcp` | full `odhcpd` (**not** `odhcpd-ipv6only`, the default on 25.12) serving DHCPv4 | no |

Note that the procedures from `rpcd-mod-luci` live on the **`luci-rpc`**
object. The separate `luci` object belongs to LuCI's ucode backend and does
not have them.

A missing object raises `UbusObjectNotFoundError`; a missing procedure raises
`UbusMethodNotFoundError`. Both are subclasses of `UbusNotFoundError`.

## Connecting

```python
import aiohttp, ssl
from aioubus import UbusClient

# HTTPS with uhttpd's default self-signed certificate:
https_client = UbusClient("192.168.1.1", "root", "password", scheme="https", verify_ssl=False)

# HTTPS trusting a specific certificate:
ctx = ssl.create_default_context(cafile="/path/to/uhttpd.crt")
pinned_client = UbusClient("router.lan", "root", "password", scheme="https", ssl_context=ctx)

# Custom port, endpoint path, request timeout and session lifetime:
tuned_client = UbusClient(
    "192.168.1.1",
    "root",
    "password",
    port=8080,
    path="/ubus",
    timeout=10.0,
    session_timeout=600,
)

# Share an existing aiohttp session (it will not be closed by the client):
session = aiohttp.ClientSession()
shared_client = UbusClient("192.168.1.1", "root", "password", session=session)
```

| Argument | Default | Meaning |
|---|---|---|
| `scheme` | `"http"` | `"http"` or `"https"` |
| `port` | scheme default | TCP port |
| `path` | `"/ubus"` | endpoint path (uhttpd's `-u` option) |
| `verify_ssl` | `True` | verify the server certificate for HTTPS |
| `ssl_context` | `None` | custom `ssl.SSLContext`; overrides `verify_ssl` |
| `timeout` | `10.0` | total timeout per HTTP request, in seconds |
| `session_timeout` | `None` (rpcd default, 300 s) | idle timeout requested at login |
| `session` | `None` | an `aiohttp.ClientSession` to use; created and owned by the client otherwise |

The client is an async context manager. `close()` closes the HTTP session only
if the client created it.

```python
async with UbusClient("192.168.1.1", "root", "password") as scoped:
    print((await scoped.get_system_board()).model)
# Without `async with`, call `await scoped.close()` when done.
```

`client.url` is the endpoint URL, and `client.logged_in` tells whether the
client holds a session that it believes to be valid.

## Sessions and expiry

You do not need to log in explicitly: the first call does it.

```python
info = await client.login()  # SessionInfo(timeout=300, expires=299, acls={...})
info.acls["ubus"]["luci-rpc"]  # ('getHostHints', 'getDHCPLeases', ...)
info = await client.renew_session()  # force a fresh session
```

`login()` raises `UbusAuthenticationError` for wrong credentials. The session
token is never exposed, logged or included in error messages, and neither is
the password.

rpcd sessions expire after `timeout` seconds without use (300 s by default;
every authorized call restarts the timer) and are lost when rpcd restarts or
the router reboots. The client handles this as follows:

1. It tracks the expected expiry locally and logs in again before a call if
   the session has lapsed.
2. If the web server still rejects the session — JSON-RPC error `-32002`
   *Access denied* (or `-32001` *Session not found*) — it logs in **once**
   and retries the call **once**. Concurrent calls that are rejected at the
   same time share that one re-login.
3. If the retry is rejected again, the session is valid but lacks access:
   `UbusPermissionError` is raised. If the re-login fails, the error from
   the login is raised (e.g. `UbusAuthenticationError`).

An expired session is indistinguishable from an ACL denial at the HTTP layer
(both are `-32002`), which is why the one retry is needed. A ubus
`PERMISSION_DENIED` *status* (6) returned by a procedure is not retried: uhttpd
has already validated the session before the procedure runs, so status 6
means a genuine denial (e.g. `file.read` of a path outside the ACL).

## API

All MAC addresses returned by the library are normalized to **lowercase,
colon-separated** form (`aa:bb:cc:dd:ee:ff`), regardless of how the
procedure reports them (`getHostHints` uses uppercase, odhcpd uses
`aabbccddeeff`). `aioubus.normalize_mac()` applies the same normalization.

### `get_host_hints()` — all known hosts

`luci-rpc getHostHints`. Merges the kernel neighbour table (ARP/NDP), DHCP
leases, `/etc/ethers`, static leases from UCI, the router's own interfaces,
and reverse DNS. Requires `rpcd-mod-luci`.

```python
hints = await client.get_host_hints()  # dict[str, HostHint], keyed by MAC
for mac, host in hints.items():
    print(mac, host.name, host.ipv4_addresses, host.ipv6_addresses)
```

`HostHint` has `mac`, `ipv4_addresses`, `ipv6_addresses` and `name` (`None`
if no source supplied a hostname). The hints say nothing about whether a host
is wired or wireless, or online right now: a static lease for a device that
is switched off appears with empty address lists, and the router's own
interfaces are included. Combine with `get_hostapd_clients()` for association
state.

### `get_dhcp_leases(family=0)` — active DHCP leases

`luci-rpc getDHCPLeases`. Reads the dnsmasq and odhcpd lease files.
`family` is `0` (both), `4` or `6`. Requires `rpcd-mod-luci`.

```python
for lease in await client.get_dhcp_leases(family=4):
    print(lease.mac, lease.ip_address, lease.hostname, lease.expires)
```

`DhcpLease`: `family`, `ip_address`, `mac`, `hostname`, `expires` (seconds
remaining; `None` for an infinite lease), `interface`, `duid`, `iaid`,
`ipv6_addresses`.

### `get_hostapd_clients(interface)` and `list_hostapd_interfaces()`

`hostapd.<iface> get_clients`, and the list of `hostapd.*` objects. These
report association, authorization and signal strength, which host hints
lack, and work without `rpcd-mod-luci`. Listing needs no session.

```python
for iface in await client.list_hostapd_interfaces():  # e.g. ('phy0-ap0', 'phy1-ap0')
    result = await client.get_hostapd_clients(iface)  # 'hostapd.' prefix optional
    for mac, station in result.clients.items():
        print(iface, result.frequency, mac, station.authorized, station.signal)
```

`HostapdClients`: `interface`, `frequency` (MHz; `None` for wired 802.1X
objects), `clients` (keyed by MAC). `HostapdClient`: `authorized`,
`authenticated`, `associated`, `aid`, `signal` (dBm), `rx_bytes`/`tx_bytes`,
`rx_packets`/`tx_packets`, `rx_rate`/`tx_rate` (kbit/s), capability flags
(`wmm`, `ht`, `vht`, `he`, `mfp`, …) and the untouched entry in `raw`. Every
station is returned; filtering on `authorized` is up to you.

### `get_wireless_devices()` and `get_network_devices()`

`luci-rpc getWirelessDevices` (radios with their interfaces and iwinfo) and
`luci-rpc getNetworkDevices` (kernel network devices). Require
`rpcd-mod-luci`. On a device without Wi-Fi, `get_wireless_devices()` raises
`UbusNotFoundError`.

```python
for name, radio in (await client.get_wireless_devices()).items():
    print(name, radio.up, [(i.ifname, i.ssid, i.mode) for i in radio.interfaces])

for name, dev in (await client.get_network_devices()).items():
    print(name, dev.mac, dev.up, dev.devtype, dev.ipv4_addresses, dev.stats.get("rx_bytes"))
```

### `get_system_board()` and `get_board_json()` — device info

`system board` (procd, always present) gives model, hostname and **firmware
version**. `luci-rpc getBoardJSON` returns `/etc/board.json`: hardware
description (model id/name, default network layout), but **no firmware
version**.

```python
board = await client.get_system_board()
print(board.model, board.hostname, board.release.version, board.release.revision)

board_json = await client.get_board_json()
print(board_json.model_id, board_json.model_name, board_json.raw.get("network"))
```

### `uci_get_config()`, `uci_get_section()`, `uci_get_option()`

`uci get` at the three levels it supports. Option values are `str`, or
`tuple[str, ...]` for UCI lists.

```python
sections = await client.uci_get_config("dhcp", section_type="dnsmasq")
lan = await client.uci_get_section("dhcp", "lan")
print(lan.type, lan.options.get("leasetime"))
iface = await client.uci_get_option("dhcp", "lan", "interface")  # 'lan'
```

`UciSection`: `name`, `type`, `anonymous`, `index`, `options`. Sections
from `uci_get_config` are ordered as in the file.

### `file_read()` and `file_read_bytes()`

`file read`. Requires `rpcd-mod-file`, and the path must be allowed by the
session's `file` ACL (otherwise `UbusPermissionError`). This applies to
`root` too: root's ACL is the union of the installed LuCI ACL files, which
include `/tmp/dhcp.leases` and `/etc/board.json` but not, for example,
`/etc/openwrt_release`. rpcd refuses files of 256 KiB or more
(`UbusNotSupportedError`).

```python
text = await client.file_read("/tmp/dhcp.leases")
data = await client.file_read_bytes("/etc/board.json")  # base64 transfer
```

### `get_dnsmasq_leases(path=None)`

Reads and parses dnsmasq's lease file. Without `path`, it uses the
`leasefile` option of the first `dnsmasq` section in `/etc/config/dhcp`
(falling back to `/tmp/dhcp.leases`). `parse_dnsmasq_leases(text)` is
available separately.

```python
for lease in await client.get_dnsmasq_leases():
    print(lease.mac, lease.ip_address, lease.hostname, lease.expires_at)
```

`DnsmasqLease`: `expires_at` (Unix time; `None` if infinite), `mac` (`None`
for non-Ethernet hardware addresses), `hwaddr` (as written), `ip_address`,
`hostname`, `client_id`.

### `get_odhcpd_ipv4_leases()`

odhcpd's `dhcp ipv4leases`, for setups where odhcpd serves DHCPv4. Requires
the full `odhcpd` package; with `odhcpd-ipv6only` the method does not exist
and `UbusMethodNotFoundError` is raised.

```python
for lease in await client.get_odhcpd_ipv4_leases():
    print(lease.interface, lease.mac, lease.ip_address, lease.hostname, lease.flags)
```

`OdhcpdLease`: `interface`, `mac`, `ip_address`, `hostname` (`None` when
odhcpd reports an empty name), `valid` (seconds; `None` if infinite),
`flags` (`bound`, `static`, `broken-hostname`), `accept_reconfigure`, `duid`,
`iaid`.

### `call()` — anything else

The escape hatch for procedures without a wrapper. Arguments can be given as
a mapping, keywords, or both; it returns the raw payload, or `None` for
procedures that return nothing. Session handling is the same as for the
typed methods.

```python
info = await client.call("system", "info")
await client.call("uci", "get", {"config": "network", "section": "lan"})
await client.call("luci-rpc", "getDHCPLeases", family=4)
```

### `list_objects()` and `list_object_names()`

```python
names = await client.list_object_names()  # ('dhcp', 'file', 'luci-rpc', ...)
signatures = await client.list_objects("luci-rpc", "file")
signatures["file"]["read"]  # {'path': 'string', 'base64': 'boolean', ...}
```

Neither needs a session. Patterns accept a trailing `*`.

### Clean up

```python
await client.close()
```

## Errors

```
UbusError                          base; .status (ubus status) and .rpc_code (JSON-RPC code)
├── UbusConnectionError            network failure
│   ├── UbusTimeoutError           local timeout, ubus TIMEOUT (7), JSON-RPC -32003
│   ├── UbusSSLError               TLS handshake / certificate failure
│   └── UbusHttpError              non-2xx HTTP status (.http_status), e.g. wrong path → 404
├── UbusAuthenticationError        login rejected
├── UbusPermissionError            PERMISSION_DENIED (6), or -32002 after a successful re-login
├── UbusResponseError              invalid JSON or unexpected payload shape
└── UbusCallError                  any other non-zero status / JSON-RPC error
    ├── UbusInvalidArgumentError   INVALID_COMMAND (1), INVALID_ARGUMENT (2), -32602
    ├── UbusNotFoundError          NOT_FOUND (4)
    │   ├── UbusObjectNotFoundError   -32000 (object does not exist)
    │   └── UbusMethodNotFoundError   METHOD_NOT_FOUND (3)
    ├── UbusNoDataError            NO_DATA (5)
    └── UbusNotSupportedError      NOT_SUPPORTED (8)
```

No `aiohttp` or `json` exception escapes unwrapped (the original is kept as
`__cause__`), and a malformed or truncated payload raises
`UbusResponseError` rather than `TypeError` or `KeyError`.

## Minimal ACL for a non-root account

Home Assistant does not need root. This was tested on OpenWrt 25.12.5 and
24.10.8. Create `/usr/share/rpcd/acl.d/aioubus.json`:

```json
{
	"aioubus": {
		"description": "Read-only access for aioubus / Home Assistant",
		"read": {
			"ubus": {
				"luci-rpc": [ "getHostHints", "getDHCPLeases", "getWirelessDevices", "getNetworkDevices", "getBoardJSON" ],
				"hostapd.*": [ "get_clients" ],
				"dhcp": [ "ipv4leases" ],
				"system": [ "board" ],
				"uci": [ "get" ],
				"file": [ "read" ]
			},
			"uci": [ "dhcp", "wireless" ],
			"file": {
				"/tmp/dhcp.leases": [ "read" ]
			}
		}
	}
}
```

Then add a login to `/etc/config/rpcd` and restart rpcd:

```sh
HASH=$(uhttpd -m 'a-strong-password')
cat >> /etc/config/rpcd <<EOF

config login
	option username 'hass'
	option password '$HASH'
	list read 'aioubus'
EOF
/etc/init.d/rpcd restart
```

How the ACL works:

- `ubus` grants the procedures; uhttpd checks it before every call. Remove
  the ones you do not use.
- `uci` grants read access to those config files for `uci get`.
- `file` grants paths for `file read`. If you change dnsmasq's `leasefile`,
  grant that path instead.
- Login itself is allowed by rpcd's built-in `unauthenticated` ACL
  (`session.login`); keep it.

With this ACL, calls outside it raise `UbusPermissionError`. `file.read` of
another path returns status 6 and is not retried; a procedure missing from
`ubus` returns `-32002` and costs one re-login before the error is raised.

## Protocol notes

Taken from the sources and from captured traffic (see *Verification* below):

- The endpoint takes JSON-RPC 2.0 POSTs. `call` takes
  `params: [session, object, procedure, args]` and answers
  `result: [status]` or `result: [status, payload]`. uhttpd and nginx answer
  HTTP 200 even for errors.
- Errors detected by the web server before the procedure runs come back as a
  JSON-RPC `error` object, not as a status: `-32000` object not found,
  `-32002` access denied (also expired or unknown session), `-32003`
  timeout, `-32700` parse error. nginx sets `"id": null` in these.
- `list` needs no session. Without `params` it returns an array of object
  names. With `params` (an array of patterns) it returns
  `{object: {method: {arg: type}}}`; an empty `params` array returns `{}`.
- rpcd-mod-luci reports an infinite DHCP lease as `"expires": false`; odhcpd
  reports one as `"valid": -1`.

## Verification

| Area | Basis |
|---|---|
| Login, wrong credentials, session expiry (`-32002`), object/method not found, invalid argument, procedure `PERMISSION_DENIED`, no-payload results, request parse errors | Captured from OpenWrt 25.12.5 and 24.10.8 (uhttpd) and 24.10.8 (nginx-mod-ubus), and exercised live by `tests/test_live.py` on all three over HTTP and, for uhttpd, HTTPS |
| `getHostHints`, `getDHCPLeases`, `getNetworkDevices`, `getBoardJSON`, `system board`, `uci get`, `file read`, `list` | Captured from real devices with real DHCP clients (Docker `openwrt/rootfs` x86-64 images) |
| `dhcp ipv4leases` | Captured from 25.12.5 running full odhcpd with real DHCP clients |
| The minimal ACL above | Tested live with a restricted account on 25.12.5 and 24.10.8 |
| `hostapd.* get_clients` (2.4 GHz AP, associated stations), `getWirelessDevices` with AP and station interfaces | Captured from 25.12.5 in the QEMU lab (`real_test/`), where the radios are virtual (`mac80211_hwsim`) and five stations really associate with hostapd, and exercised live by `tests/test_live.py`. Signal and rates there come from the simulator, not from a real driver. |
| `hostapd.*` for wired 802.1X, 5 GHz, unauthorized stations, MFP | From source only (`hostapd/src/src/ap/ubus.c` in openwrt/openwrt, `luci.c` in openwrt/luci). The models keep the untouched payload in `raw`. |
| Infinite leases (`expires: false`, `valid: -1`), DHCPv6 leases, odhcpd's pre-2024 field names | From source only |
| Array-wrapped `list` results | From unreleased nginx-ubus-module HEAD only. OpenWrt ships revision `b2d7260`, which was captured and matches uhttpd. |

The fixtures are in `tests/fixtures/`: `openwrt-*` are raw captured bodies,
and `derived/` cites the source each one was written from;
`openwrt-25.12.5-hwsim` comes from the QEMU lab (`real_test/lab.sh capture`).
To capture from your own device, run `scripts/capture_fixtures.py`.
Contributions of hostapd captures from real Wi-Fi hardware are welcome.

## Development

```bash
uv sync
uv run pytest                 # unit tests: a local HTTP/HTTPS server replays captured bodies
uv run ruff check . && uv run ruff format --check .
uv run mypy                   # strict
AIOUBUS_LIVE_URL=http://192.168.1.1/ubus AIOUBUS_LIVE_PASSWORD=... uv run pytest -m live
```

For live tests without a spare router, `real_test/lab.sh up && real_test/lab.sh test`
boots an OpenWrt VM with fake wired and Wi-Fi clients (virtual radios) and
runs the live suite against it; see
[real_test/openwrt-presence-testlab.md](real_test/openwrt-presence-testlab.md).

Releases follow semantic versioning and are published to PyPI from GitHub
releases with trusted publishing (`.github/workflows/release.yml`); no API
token is stored.

## License

Apache-2.0
