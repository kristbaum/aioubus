# OpenWrt test lab (QEMU)

A throwaway OpenWrt 25.12 VM on your machine, with fake wired and Wi-Fi
clients, for testing aioubus against a real rpcd/uhttpd, and later a Home
Assistant ubus integration. `lab.sh` drives the VM. `fakedevs.sh` creates the
fake devices. `lab.env` holds the settings both scripts use.

## Quick start

Requirements: Linux with KVM, `qemu-system-x86_64`, `busybox` (its `udhcpc`
gives the wired fakes DHCP leases), `uv`, and sudo. `curl`, `sfdisk` and
`debugfs` are used too. They come with every Debian/Ubuntu install
(util-linux and e2fsprogs are priority "required").

```bash
sudo apt install --no-install-recommends qemu-system-x86   # Debian/Ubuntu
command -v busybox || sudo apt install busybox-static      # usually already there

./real_test/lab.sh up      # first run downloads, boots, installs Wi-Fi packages, reboots
./real_test/lab.sh test    # fake devices up, then tests/test_live.py over HTTP and HTTPS
./real_test/lab.sh stop    # or: reset (fresh disk next time), destroy (remove everything)
```

Run it as your normal user. It calls `sudo` only to create the bridge and tap
and to run `fakedevs.sh`. QEMU runs unprivileged. Every command can be re-run
safely: `up` skips the steps that are already done, so after `stop`, running
`up` again just boots the VM.

| | |
|---|---|
| ubus endpoint | `http://192.168.77.1/ubus`, `https://192.168.77.1/ubus` (self-signed) |
| LuCI | <http://192.168.77.1/> |
| root | `root` / `testlab-root` |
| restricted user | `hass` / `testlab-hass`, with the README's minimal `aioubus` ACL |
| Wi-Fi AP | SSID `testnet`, PSK `testtest`, 2.4 GHz channel 1, on `radio0` |
| ssh | `./real_test/lab.sh ssh` (key in `real_test/.lab/id_ed25519`) |

The lab uses `192.168.77.0/24`, not OpenWrt's default `192.168.1.0/24`, so
it cannot collide with a real router. To use another subnet, set `LAB_NET`
(for example `LAB_NET=10.99.0 ./real_test/lab.sh up`) **before the first
`up`**, or run `reset` afterwards: the router's IP is written into the disk
at creation. All other settings are in [lab.env](lab.env).

## What `lab.sh test` checks

It brings every fake device up, waits until the Wi-Fi fakes have associated,
and runs `pytest -m live tests/test_live.py` twice, over `http` and over
`https`. It passes these variables:

| Variable | Value in the lab |
|---|---|
| `AIOUBUS_LIVE_URL` | `http://192.168.77.1/ubus`, then `https://...` |
| `AIOUBUS_LIVE_PASSWORD` | root password |
| `AIOUBUS_LIVE_ACL_USER` / `_PASSWORD` | restricted `hass` account |
| `AIOUBUS_LIVE_EXPECT_WIRED` | `mac=name,...` of the wired fakes that are up |
| `AIOUBUS_LIVE_EXPECT_WIFI` | `mac=name,...` of the Wi-Fi fakes that are up |

Together the live tests cover:

- login, every typed wrapper, `list`, and session expiry followed by re-login
- the restricted ACL: allowed calls work, `file.read /etc/shadow` and
  `luci.setLocaltime` raise `UbusPermissionError`
- wired fakes appear in `getHostHints` with an IPv4 address and their DHCP
  hostname, and in the dnsmasq lease file read through `file.read`
- Wi-Fi fakes appear in `hostapd.* get_clients` as authorized and associated
  on 2412 MHz, and `getWirelessDevices` returns radios. Before this lab these
  payloads were checked against source only (see the README's
  *Verification* table).

Arguments after `test` are passed on to pytest: `./real_test/lab.sh test -k wifi -x`.

To run pytest yourself, for example from an IDE, load the variables first:

```bash
eval "$(./real_test/lab.sh env)"          # or: lab.sh env https
uv run pytest -m live tests/test_live.py
```

The `EXPECT` values describe the fakes that are up when `env` runs. If none
are up, the presence tests are skipped.

## Topology

```
                            host (Linux)
 ┌──────────────────────────────────────────────────────────────────┐
 │ br-owrt 192.168.77.2/24                                          │
 │  ├── tap-owrt ──────────── eth0 = LAN  br-lan 192.168.77.1       │
 │  ├── fdv0 ── netns fd-nas          ┐                             │
 │  ├── fdv1 ── netns fd-hue-bridge   ├─ wired fakes (DHCP clients) │
 │  └── ...                           ┘                             │
 │                                                                  │
 │ QEMU user-net ──────────── eth1 = WAN  (internet, for apk)       │
 └──────────────────────────────────────────────────────────────────┘
   inside the VM (mac80211_hwsim, radios=8):
     radio0      = AP "testnet", bridged into br-lan → hostapd.phy0-ap0 (typically)
     radio1..5   = Wi-Fi fakes as stations (radio6, radio7 spare)
```

Wired fakes are network namespaces on the host bridge. Each gets a real
DHCP lease from dnsmasq and pings the router every 5 s, so the router's
neighbour table and lease file see them. Wi-Fi fakes are station interfaces
on virtual radios inside the VM. `mac80211_hwsim` must run in the guest
kernel, which is why this is a VM and not a container. The stations really
associate with hostapd, so `get_clients` returns real data.

## Fake devices

```bash
./real_test/lab.sh fakes list              # the fleet (no root needed)
./real_test/lab.sh fakes up all            # or: wired | wifi | <name>
./real_test/lab.sh fakes down iphone --hard
./real_test/lab.sh fakes wait 60           # until all Wi-Fi fakes that are up have associated
./real_test/lab.sh fakes status            # host view, hostapd associations, leases, neighbours
./real_test/lab.sh fakes churn 20 30       # every 20 s toggle a random "mobile" device, 30 rounds
```

`lab.sh fakes ...` runs `sudo real_test/fakedevs.sh ...`. The default fleet:

| Name | Medium | Role |
|---|---|---|
| nas, hue-bridge, raspberrypi, sonos-wired | wired | static |
| desktop | wired | mobile |
| iphone, android, laptop | wifi | mobile |
| esp32-sensor, nest-thermostat | wifi | static |

To change the fleet, edit the `DEVICES` table at the top of `fakedevs.sh`.
The script supports up to 7 Wi-Fi fakes, one per spare radio. After changing
Wi-Fi entries, run `./real_test/lab.sh fakes setup` again.

What the router remembers after a device leaves, so a test does not expect
it to disappear:

- `down` stops the device. Its DHCP lease stays in `/tmp/dhcp.leases` until
  it expires (12 h), and `getHostHints` keeps the host as long as the lease
  or a neighbour entry exists. This is the behaviour of a real router.
- `down --hard` also deletes the device's neighbour entry, so ARP-based
  presence drops at once instead of after ARP expiry.
- Wi-Fi fakes have no IP address. The station interfaces are not attached to
  a network. They show up only in `hostapd.* get_clients`, not in
  `getHostHints` or the leases. A Wi-Fi fake that leaves disappears from
  `get_clients` at once.

## Workflows

**After changing the library:** run `lab.sh up` once (or after a reboot of
the host), then `lab.sh test` for each change. The unit tests (`uv run pytest`) remain the main suite. The lab
checks the library against real devices.

**Capture fixtures with Wi-Fi data:** `./real_test/lab.sh capture` brings
the fakes up and runs `scripts/capture_fixtures.py` against the lab. The
output goes to `real_test/.lab/captures/openwrt-<version>-hwsim/`, including
`hostapd_get_clients_phy0-ap0.json`. Review the files, then copy the useful
ones into `tests/fixtures/`.

**Home Assistant (ubus integration):** HA must reach `192.168.77.1`, which
exists only on this host. Run HA Core or the HA container on the same host;
with Docker, use `--network host`. Configure the integration with host `192.168.77.1`
and the `hass` / `testlab-hass` account. Its ACL is the one the README
documents for Home Assistant. For the legacy YAML tracker:

```yaml
device_tracker:
  - platform: ubus
    host: 192.168.77.1
    username: hass
    password: testlab-hass
```

Then run `./real_test/lab.sh fakes churn 20` and watch the `device_tracker`
entities change state. Wired departures with `--hard` and Wi-Fi departures
should show up within one scan interval. Wired departures without `--hard`
show up only after the neighbour entry expires, and lease-based sources
never notice them.

## How it works

`lab.sh up` runs these steps, each skipped if already done:

1. **image:** downloads `openwrt-25.12.5-x86-64-generic-ext4-combined.img.gz`,
   checks its sha256, and copies it to `.lab/disk.img`. It then writes
   `/etc/uci-defaults/99-testlab` into the rootfs partition with `debugfs`
   (no root and no loop mount needed). On first boot that script sets the
   root password, installs the lab ssh key, and sets the LAN IP. A fresh
   ssh key is created in `.lab/`.
2. **net:** creates `br-owrt` with the host on `.2`, and `tap-owrt` owned by
   your user. If `br_netfilter` is active, which Docker enables, it also adds
   an iptables `FORWARD` accept rule for the bridge. Without it, Docker's
   DROP policy discards the bridged DHCP traffic.
3. **start:** boots QEMU in the background (`.lab/qemu.pid`) with the serial
   console logged to `.lab/serial.log`, and waits for ssh.
4. **provision:** runs `apk add kmod-mac80211-hwsim wpad-mbedtls iw`, sets
   `mac80211_hwsim radios=8`, and installs the ACL from the README's *Minimal
   ACL* section. The ACL is extracted from README.md, so the lab tests what
   the README documents. It then adds the `hass` login, reboots, generates
   `/etc/config/wireless`, brings up the AP on `radio0`, and writes the
   disabled station configs for the Wi-Fi fakes (`fakedevs.sh setup`).
   The stock image already includes `luci-ssl`, `rpcd-mod-luci`,
   `rpcd-mod-file` and `uhttpd-mod-ubus`.

Provisioning needs internet in the VM. That goes through the WAN port and
QEMU's NAT. Afterwards the lab works offline.

To try another release, set `OPENWRT_VERSION=24.10.8` before the first
`up`, or run `reset` first. 24.10 uses `opkg`, not `apk`, so the provision
step needs adjusting for it.

## Manual checks

The curl examples use `jq` to pull out the session token.

```bash
./real_test/lab.sh ssh "ubus list 'hostapd.*'; ubus call luci-rpc getHostHints"

SID=$(curl -s http://192.168.77.1/ubus -d '{"jsonrpc":"2.0","id":1,"method":"call",
  "params":["00000000000000000000000000000000","session","login",
  {"username":"root","password":"testlab-root"}]}' | jq -r '.result[1].ubus_rpc_session')
curl -s http://192.168.77.1/ubus -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"call\",
  \"params\":[\"$SID\",\"hostapd.phy0-ap0\",\"get_clients\",{}]}" | jq
```

## Troubleshooting

- **`up` hangs waiting for ssh:** look at `./real_test/lab.sh console`. If
  the VM booted but is unreachable, check `ip -br addr show br-owrt` and
  that `tap-owrt` is a port of the bridge (`bridge link`). A disk created
  with a different `LAB_NET` keeps its old router IP: run `reset`.
  When IPv4 is broken, the router is usually still reachable over IPv6
  link-local, because its MAC address is fixed:
  `ssh -i real_test/.lab/id_ed25519 root@fe80::5054:ff:fe77:1%br-owrt`.
- **`/dev/kvm not writable`:** add yourself to the `kvm` group (log out and
  back in). Without KVM the lab still works, but much slower.
- **Wired fake gets no lease:** check for `busybox` on the host. Check the
  iptables rule from step 2 (`sudo iptables -S FORWARD | grep br-owrt`), and
  check whether a host firewall (ufw, firewalld) filters bridged traffic.
  Without a DHCP client, the script falls back to a static `.200+` address.
  That address shows up in ARP, but not in the lease file, and the wired
  presence test fails.
- **`fakes wait` times out:** run `./real_test/lab.sh ssh "logread | grep -E 'hostapd|wpa_supplicant'"`.
  All radios must share band and channel, and `setup` forces `2g`/`1`.
- **ubus `-32002` / status 6 for `hass`:** check the ACL JSON on the router
  (`/usr/share/rpcd/acl.d/aioubus.json`), then run `service rpcd restart`.
- **Start over:** run `./real_test/lab.sh reset && ./real_test/lab.sh up`.
  The download is kept. `destroy` also removes the bridge, the fakes and
  the cached image.
