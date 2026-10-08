# Migrating Home Assistant's `ubus` integration from `openwrt-ubus-rpc`

This maps each `openwrt-ubus-rpc` call used by
`homeassistant/components/ubus/device_tracker.py` to its `aioubus`
replacement. Everything becomes `async`, so the scanner methods turn into
`async_*` methods and need no executor.

## Summary

| `openwrt-ubus-rpc` (sync) | `aioubus` (async) | Returns |
|---|---|---|
| `Ubus(url, user, pw)` | `UbusClient(host, user, pw, session=async_get_clientsession(hass))` | — |
| `ubus.connect()` | `await client.login()` | `SessionInfo`; raises instead of returning `None` |
| `ubus.get_hostapd()` | `await client.list_hostapd_interfaces()` | `tuple[str, ...]` of interface names |
| `ubus.get_hostapd_clients(name)` | `await client.get_hostapd_clients(name)` | `HostapdClients` |
| `ubus.get_uci_config("dhcp", "dnsmasq")` | `await client.uci_get_config("dhcp", section_type="dnsmasq")` | `dict[str, UciSection]` |
| `ubus.file_read(path)` | `await client.file_read(path)` | `str` |
| lease-file parsing in the integration | `await client.get_dnsmasq_leases()` | `tuple[DnsmasqLease, ...]` |
| `ubus.get_dhcp_method("ipv4leases")` | `await client.get_odhcpd_ipv4_leases()` | `tuple[OdhcpdLease, ...]` |
| `_refresh_on_access_denied` decorator | delete it | handled by the library |
| *(new)* | `await client.get_host_hints()` | `dict[str, HostHint]` — wired and wireless hosts |

## Behaviour changes

- **Errors are raised, not returned as `None`.** Catch
  `UbusAuthenticationError` (wrong credentials; maps to
  `ConfigEntryAuthFailed`), `UbusConnectionError` (unreachable, timeout,
  TLS, HTTP errors; maps to `ConfigEntryNotReady`/`UpdateFailed`), and
  `UbusError` as the base for everything else.
- **Session expiry is handled inside the library.** On an expired or
  reboot-invalidated session the client logs in once and retries once. The
  `_refresh_on_access_denied` decorator and its `PermissionError` handling
  go away; a `UbusPermissionError` that does reach the integration is a real
  ACL problem.
- **MAC addresses are lowercase with colons** (`aa:bb:cc:dd:ee:ff`)
  everywhere, including odhcpd's unseparated form. Drop the integration's
  `.upper()` calls and colon insertion. Use `.lower()` (or
  `homeassistant.helpers.device_registry.format_mac`) on any MAC that does
  not come from `aioubus` before looking it up.
- **The URL is built from parts.** `http://{host}/ubus` becomes
  `UbusClient(host, ...)`; HTTPS is `scheme="https"`, plus `verify_ssl` or
  `ssl_context`.
- **Return values are typed models**, not raw dicts. Field names are shown
  below.

## Call by call

### `connect`

```python
# before (executor job)
self.ubus = Ubus(f"http://{host}/ubus", username, password)
self.success_init = self.ubus.connect() is not None

# after
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from aioubus import UbusClient, UbusAuthenticationError, UbusConnectionError

self.client = UbusClient(host, username, password, session=async_get_clientsession(hass))
try:
    await self.client.login()
except UbusAuthenticationError, UbusConnectionError:
    ...  # setup failed
```

Calling `login()` is optional: the first call logs in by itself. Calling it
during setup validates the credentials early.

### `get_hostapd`

```python
# before: dict keyed by object name, e.g. {"hostapd.wlan0": {...}}
self.hostapd.extend(self.ubus.get_hostapd().keys())

# after: interface names, e.g. ("phy0-ap0", "phy1-ap0")
self.hostapd.extend(await self.client.list_hostapd_interfaces())
```

`get_hostapd_clients()` accepts the name with or without the `hostapd.`
prefix, so stored object names keep working.

### `get_hostapd_clients`

```python
# before
if result := self.ubus.get_hostapd_clients(hostapd):
    for key in result["clients"]:
        if result["clients"][key]["authorized"]:
            self.last_results.append(key)

# after
result = await self.client.get_hostapd_clients(hostapd)
for mac, station in result.clients.items():
    if station.authorized:
        self.last_results.append(mac)
```

`HostapdClient` also has `associated`, `signal` (dBm), byte, packet and rate
counters, and the untouched entry in `raw`. Filtering on `authorized` stays
in the integration.

### `get_uci_config`

```python
# before
if result := self.ubus.get_uci_config("dhcp", "dnsmasq"):
    values = result["values"].values()
    self.leasefile = next(iter(values))["leasefile"]

# after
sections = await self.client.uci_get_config("dhcp", section_type="dnsmasq")
if sections:
    self.leasefile = next(iter(sections.values())).options.get("leasefile")
```

`UciSection.options` holds `str` values, or `tuple[str, ...]` for UCI lists.
`uci_get_section()` and `uci_get_option()` read a single section or option.

### `file_read`

```python
# before
result = self.ubus.file_read(self.leasefile)
for line in result["data"].splitlines():
    hosts = line.split(" ")
    self.mac2name[hosts[1].upper()] = hosts[3]

# after: the library resolves the lease file from UCI and parses it
for lease in await self.client.get_dnsmasq_leases():
    if lease.mac and lease.hostname:
        self.mac2name[lease.mac] = lease.hostname

# or, keeping the raw text
text = await self.client.file_read(self.leasefile)
```

`get_dnsmasq_leases()` turns dnsmasq's `*` (no hostname) into `None`, so a
host without a name no longer maps to the string `"*"`. Pass `path=` to
skip the UCI lookup.

### `get_dhcp_method("ipv4leases")`

```python
# before
if result := self.ubus.get_dhcp_method("ipv4leases"):
    for device in result["device"].values():
        for lease in device["leases"]:
            mac = lease["mac"]  # aabbccddeeff
            mac = ":".join(mac[i : i + 2] for i in range(0, len(mac), 2))
            self.mac2name[mac.upper()] = lease["hostname"]

# after
for lease in await self.client.get_odhcpd_ipv4_leases():
    if lease.hostname:
        self.mac2name[lease.mac] = lease.hostname
```

Each `OdhcpdLease` also has `interface`, `ip_address`, `valid` and `flags`.
An empty hostname from odhcpd becomes `None`.

## Recommended: `get_host_hints`

On devices with LuCI, one call replaces the hostapd scan plus the DHCP
lookup, and it also sees wired clients:

```python
hints = await self.client.get_host_hints()
names = {mac: hint.name for mac, hint in hints.items() if hint.name}
```

Host hints include stale entries (static leases, neighbour-table leftovers)
and the router's own interfaces, and do not say whether a host is online.
To track presence, keep using `get_hostapd_clients()` for wireless clients,
and use host hints for names and for discovering wired hosts.

## Requirements and ACL

`get_host_hints()` and the other `luci-rpc` calls need `rpcd-mod-luci`, which
is installed with LuCI. To run the integration without root, see the README
section *Minimal ACL for a non-root account*.
