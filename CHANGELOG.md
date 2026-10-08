# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## 0.1.0 — unreleased

First release.

- Async `UbusClient` for the ubus HTTP/JSON-RPC endpoint (uhttpd and
  nginx), over HTTP or HTTPS, with an injectable `aiohttp.ClientSession`.
- Automatic login, local expiry tracking, and a single re-login and retry
  when the server rejects the session.
- Typed wrappers: `luci-rpc` (`getHostHints`, `getDHCPLeases`,
  `getWirelessDevices`, `getNetworkDevices`, `getBoardJSON`), `system board`,
  `hostapd.* get_clients`, `uci get`, `file read`, odhcpd `ipv4leases`,
  dnsmasq lease-file parsing, object listing, and a generic `call()`.
- Exception hierarchy rooted at `UbusError`.
