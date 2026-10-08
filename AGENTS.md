# AGENTS.md

aioubus is an async Python client for OpenWrt's ubus HTTP/JSON-RPC API
(`/ubus` on uhttpd or nginx). Home Assistant integrations use it as a
dependency, so it must not import Home Assistant or contain presence or
business logic. It parses the protocol and returns typed models.

## Layout

- `src/aioubus/client.py`: `UbusClient` (transport, login, session renewal, typed wrappers)
- `src/aioubus/models.py`: frozen dataclasses for every response; `_parse.py` validates the payloads
- `src/aioubus/exceptions.py`: error hierarchy rooted at `UbusError`; `const.py` has the status codes
- `tests/`: unit tests against `FakeUbus` (`conftest.py`), a local HTTP/HTTPS server that replays fixtures
- `tests/fixtures/openwrt-*`: raw bodies captured from real devices; `derived/` holds bodies written from upstream source, with the source cited
- `scripts/capture_fixtures.py`: captures new fixtures from a live device
- `real_test/`: QEMU OpenWrt lab with fake wired and Wi-Fi clients (see below)

## Checks

CI runs all of these. Run them before you finish a change:

```bash
uv sync
uv run ruff format --check . && uv run ruff check .
uv run ty check                # type check: src, tests and scripts; warnings fail
uv run pytest                  # coverage gate in CI: --cov-fail-under=95
```

## Testing against a real router

Use the lab in `real_test/` for any change that affects what goes over the
wire or how payloads are parsed: new wrappers, model fields, error mapping,
session handling. It is documented in
[real_test/openwrt-presence-testlab.md](real_test/openwrt-presence-testlab.md).

```bash
./real_test/lab.sh up      # once: boots and provisions the OpenWrt 25.12 VM
./real_test/lab.sh test    # fake devices up, then tests/test_live.py over HTTP and HTTPS
./real_test/lab.sh stop
```

- The lab needs KVM, qemu and sudo (for the bridge and the fake devices).
  If you can't run it, for example in a sandbox, say so in your summary.
  Don't claim live verification you didn't do.
- `./real_test/lab.sh capture` records fixtures, including hostapd data
  from virtual radios, into `real_test/.lab/captures/`. Review them before
  you copy them into `tests/fixtures/`.
- rpcd's root is not unrestricted: its login grants every ACL *group*, so
  root can call only what some installed ACL grants (`-32002` otherwise),
  and `uci` access is per config name. Keep this in mind when you interpret
  a status from the lab.
- New live tests go in `tests/test_live.py`, marked `live`. They must skip
  cleanly when their `AIOUBUS_LIVE_*` variables are unset.
- The lab installs the ACL from the README's *Minimal ACL* section verbatim.
  If you change that ACL, run the lab again.

## Conventions

- Model payloads from captured traffic, not from guesses. If a shape exists
  only in upstream source, add it under `fixtures/derived/` with the source
  cited, keep the model permissive (`raw` field), and record it in the
  README's *Verification* table.
- Normalize MACs once, at the library boundary: lowercase, colon-separated.
- Never let `aiohttp`, `json`, `KeyError` or `TypeError` escape. Map them to
  a `UbusError` subclass and keep the original as `__cause__`.
- Never log credentials or session tokens.
- Don't copy code from `openwrt-ubus-rpc` or `openwrt-luci-rpc`.
- Update `README.md` (API docs, required packages, ACL) and `CHANGELOG.md`
  with every user-visible change.
