#!/usr/bin/env bash
# lab.sh - OpenWrt QEMU test lab for aioubus (and Home Assistant ubus integrations)
#
# Usage (run as your normal user; sudo is invoked only for network setup and fake devices):
#   lab.sh up              image + network + boot + provision (idempotent, safe to re-run)
#   lab.sh test [args]     bring all fake devices up, then run the live tests over HTTP and HTTPS
#                          (extra args go to pytest, e.g. `lab.sh test -k wifi`)
#   lab.sh capture         capture raw fixtures from the lab into .lab/captures/
#   lab.sh fakes <cmd...>  run fakedevs.sh (as root) with the lab settings
#   lab.sh status          VM, network and router state
#   lab.sh ssh [cmd]       shell on the router
#   lab.sh console         follow the VM's serial console log
#   lab.sh env             print the AIOUBUS_LIVE_* variables for running pytest by hand
#   lab.sh stop            power off the VM (disk state is kept)
#   lab.sh reset           stop and delete the VM disk; next `up` starts from a fresh image
#   lab.sh destroy         reset, remove fake devices, bridge and tap, and the cached download
#
# Settings live in lab.env and can be overridden from the environment.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
# shellcheck source=lab.env
source "$HERE/lab.env"

IMG_NAME="openwrt-$OPENWRT_VERSION-x86-64-generic-ext4-combined.img"
IMG_URL="https://downloads.openwrt.org/releases/$OPENWRT_VERSION/targets/x86/64"
PRISTINE="$LAB_DIR/$IMG_NAME"
DISK="$LAB_DIR/disk.img"
PIDFILE="$LAB_DIR/qemu.pid"
SERIAL="$LAB_DIR/serial.log"
KEY="$LAB_DIR/id_ed25519"
OWNER="${SUDO_USER:-$(id -un)}"

die() { echo "error: $*" >&2; exit 1; }
log() { printf '\033[1m==> %s\033[0m\n' "$*"; }

rssh() {
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR -o ConnectTimeout=3 -o BatchMode=yes "root@$ROUTER" "$@"
}

wait_ssh() { # wait_ssh <timeout_s>
  local deadline=$((SECONDS + $1))
  until rssh true 2>/dev/null; do
    (( SECONDS < deadline )) || die "router not reachable via ssh at $ROUTER (see: lab.sh console)"
    sleep 2
  done
}

vm_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

fakedevs() { sudo --preserve-env=LAB_NET,ROUTER,BRIDGE,SSID,PSK,LAB_DIR "$HERE/fakedevs.sh" "$@"; }

# ---------------------------------------------------------------------------
# image: download once, then inject a first-boot script into a working copy
# ---------------------------------------------------------------------------
cmd_image() {
  mkdir -p "$LAB_DIR"
  [[ -f "$KEY" ]] || ssh-keygen -q -t ed25519 -N '' -C openwrt-testlab -f "$KEY"

  if [[ ! -f "$PRISTINE" ]]; then
    log "downloading OpenWrt $OPENWRT_VERSION"
    curl -fL --progress-bar -o "$PRISTINE.gz" "$IMG_URL/$IMG_NAME.gz"
    (cd "$LAB_DIR" && curl -fsSL "$IMG_URL/sha256sums" | grep " \*$IMG_NAME.gz\$" | sha256sum -c -) \
      || die "checksum mismatch"
    gunzip -f "$PRISTINE.gz" || [[ -f "$PRISTINE" ]] # exit 2 = "trailing garbage ignored"
  fi

  [[ -f "$DISK" ]] && { log "disk exists ($DISK), keeping it"; return; }
  log "preparing disk"
  cp "$PRISTINE" "$DISK"

  # Runs once on first boot, before the network comes up.
  local first_boot="$LAB_DIR/99-testlab"
  cat > "$first_boot" <<EOF
#!/bin/sh
printf '%s\n%s\n' '$ROOT_PASSWORD' '$ROOT_PASSWORD' | passwd root
echo '$(cat "$KEY.pub")' > /etc/dropbear/authorized_keys
chmod 600 /etc/dropbear/authorized_keys
uci set network.lan.ipaddr='$ROUTER'
uci commit network
exit 0
EOF
  chmod 755 "$first_boot"

  # Write into the rootfs (partition 2) without root, via debugfs' offset option.
  local start
  start="$(sfdisk -d "$DISK" | sed -n 's/^.*2 : start= *\([0-9]*\),.*$/\1/p')"
  [[ -n "$start" ]] || die "could not find the rootfs partition in $DISK"
  /usr/sbin/debugfs -w -R "write $first_boot /etc/uci-defaults/99-testlab" \
    "$DISK?offset=$((start * 512))" >/dev/null 2>&1 || die "debugfs could not write to image"
}

# ---------------------------------------------------------------------------
# host network: bridge with the host on .2, plus a tap for the VM's LAN port
# ---------------------------------------------------------------------------
cmd_net() {
  if ip link show "$BRIDGE" >/dev/null 2>&1 && ip link show "$TAP" >/dev/null 2>&1; then
    return
  fi
  log "creating $BRIDGE ($HOST_IP/24) and $TAP (sudo)"
  sudo sh -euc "
    ip link show '$BRIDGE' >/dev/null 2>&1 || {
      ip link add '$BRIDGE' type bridge
      ip addr add '$HOST_IP/24' dev '$BRIDGE'
      ip link set '$BRIDGE' up
    }
    ip link show '$TAP' >/dev/null 2>&1 || {
      ip tuntap add '$TAP' mode tap user '$OWNER'
      ip link set '$TAP' master '$BRIDGE' up
    }
    # With br_netfilter loaded (Docker does this) bridged frames traverse the
    # iptables FORWARD chain, whose policy Docker sets to DROP.
    if command -v iptables >/dev/null && [ \"\$(sysctl -n net.bridge.bridge-nf-call-iptables 2>/dev/null)\" = 1 ]; then
      iptables -C FORWARD -i '$BRIDGE' -o '$BRIDGE' -j ACCEPT 2>/dev/null \
        || iptables -I FORWARD -i '$BRIDGE' -o '$BRIDGE' -j ACCEPT
    fi
  "
}

# ---------------------------------------------------------------------------
# VM
# ---------------------------------------------------------------------------
cmd_start() {
  vm_running && return
  [[ -f "$DISK" ]] || cmd_image
  cmd_net
  command -v qemu-system-x86_64 >/dev/null || die "qemu-system-x86_64 not installed (apt install --no-install-recommends qemu-system-x86)"
  local accel=(-enable-kvm -cpu host)
  [[ -w /dev/kvm ]] || { echo "warning: /dev/kvm not writable, falling back to slow emulation"; accel=(); }

  log "booting VM (serial log: $SERIAL)"
  # eth0 = LAN on the host bridge, eth1 = WAN via QEMU user-net (internet for apk)
  qemu-system-x86_64 "${accel[@]}" -m 512 -smp 2 \
    -display none -daemonize -pidfile "$PIDFILE" -serial "file:$SERIAL" \
    -drive "file=$DISK,format=raw,if=virtio" \
    -netdev "tap,id=lan,ifname=$TAP,script=no,downscript=no" \
    -device virtio-net-pci,netdev=lan,mac=52:54:00:77:00:01 \
    -netdev user,id=wan \
    -device virtio-net-pci,netdev=wan,mac=52:54:00:77:00:02
  wait_ssh 120
  log "router up at $ROUTER"
}

cmd_stop() {
  vm_running || return 0
  log "stopping VM"
  rssh poweroff 2>/dev/null || true
  local i
  for i in $(seq 30); do vm_running || break; sleep 1; done
  vm_running && kill "$(cat "$PIDFILE")"
  rm -f "$PIDFILE"
}

# ---------------------------------------------------------------------------
# provisioning: Wi-Fi packages, 8 virtual radios, AP, restricted ACL user
# ---------------------------------------------------------------------------
cmd_provision() {
  if rssh test -f /etc/testlab-provisioned; then return; fi

  log "installing packages and configuring rpcd ACL"
  # The README's minimal ACL, so the restricted-user tests use exactly what is documented.
  local acl
  acl="$(awk '/^## Minimal ACL/{f=1} f&&/^```json$/{g=1;next} g&&/^```$/{exit} g' "$REPO/README.md")"
  [[ -n "$acl" ]] || die "could not extract the ACL from README.md"

  rssh sh -es <<EOF
apk update >/dev/null
apk add kmod-mac80211-hwsim wpad-mbedtls iw >/dev/null
f=\$(grep -l '^mac80211_hwsim' /etc/modules.d/* 2>/dev/null | head -n1)
echo 'mac80211_hwsim radios=8' > "\${f:-/etc/modules.d/50-mac80211-hwsim}"

cat > /usr/share/rpcd/acl.d/aioubus.json <<'ACL'
$acl
ACL
uci -q delete rpcd.testlab || true
uci set rpcd.testlab=login
uci set rpcd.testlab.username='$ACL_USER'
uci set rpcd.testlab.password="\$(uhttpd -m '$ACL_PASSWORD')"
uci add_list rpcd.testlab.read='aioubus'
uci commit rpcd
EOF

  log "rebooting to load 8 hwsim radios"
  rssh reboot || true
  sleep 10
  wait_ssh 120

  log "configuring AP '$SSID' on radio0"
  rssh sh -es <<EOF
rm -f /etc/config/wireless
wifi config
uci -q get wireless.radio7 >/dev/null || { echo 'expected radio0..radio7' >&2; exit 1; }
for i in 1 2 3 4 5 6 7; do
  uci set wireless.radio\$i.disabled='1'
  uci -q delete wireless.default_radio\$i || true
done
uci set wireless.radio0.disabled='0'
uci set wireless.radio0.band='2g'
uci set wireless.radio0.channel='1'
uci set wireless.default_radio0.network='lan'
uci set wireless.default_radio0.mode='ap'
uci set wireless.default_radio0.ssid='$SSID'
uci set wireless.default_radio0.encryption='psk2'
uci set wireless.default_radio0.key='$PSK'
uci commit wireless
wifi
for i in \$(seq 30); do [ -n "\$(ubus list 'hostapd.*')" ] && break; sleep 1; done
[ -n "\$(ubus list 'hostapd.*')" ] || { echo 'hostapd did not come up' >&2; exit 1; }
EOF

  log "writing Wi-Fi fake station configs"
  fakedevs setup
  rssh touch /etc/testlab-provisioned
}

cmd_up() {
  cmd_image
  cmd_start
  cmd_provision
  log "lab ready: http(s)://$ROUTER/ubus  root/$ROOT_PASSWORD  $ACL_USER/$ACL_PASSWORD"
}

# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
live_env() { # live_env <scheme>
  echo "AIOUBUS_LIVE_URL=$1://$ROUTER/ubus"
  echo "AIOUBUS_LIVE_PASSWORD=$ROOT_PASSWORD"
  echo "AIOUBUS_LIVE_ACL_USER=$ACL_USER"
  echo "AIOUBUS_LIVE_ACL_PASSWORD=$ACL_PASSWORD"
  echo "AIOUBUS_LIVE_EXPECT_WIRED=$("$HERE/fakedevs.sh" expect wired)"
  echo "AIOUBUS_LIVE_EXPECT_WIFI=$("$HERE/fakedevs.sh" expect wifi)"
}

cmd_test() {
  vm_running || die "lab not running (lab.sh up)"
  log "bringing fake devices up"
  fakedevs up all
  fakedevs wait 60

  local scheme rc=0
  for scheme in http https; do
    log "live tests over $scheme"
    (cd "$REPO" && env $(live_env "$scheme") uv run python -m pytest -m live tests/test_live.py -v "$@") || rc=1
  done
  (( rc == 0 )) && log "all live tests passed" || die "live tests failed"
}

cmd_capture() {
  vm_running || die "lab not running (lab.sh up)"
  fakedevs up all
  fakedevs wait 60
  local out="$LAB_DIR/captures/openwrt-$OPENWRT_VERSION-hwsim"
  (cd "$REPO" && AIOUBUS_PASSWORD="$ROOT_PASSWORD" AIOUBUS_ACL_USER="$ACL_USER" \
    AIOUBUS_ACL_PASSWORD="$ACL_PASSWORD" uv run python scripts/capture_fixtures.py \
    "http://$ROUTER/ubus" "$out" "$OPENWRT_VERSION (QEMU x86-64, mac80211_hwsim)")
  echo "review, then copy what is useful into tests/fixtures/"
}

cmd_status() {
  echo "VM:      $(vm_running && echo "running (pid $(cat "$PIDFILE"))" || echo stopped)"
  echo "disk:    $([[ -f "$DISK" ]] && echo "$DISK" || echo none)"
  echo "network: $(ip -br addr show "$BRIDGE" 2>/dev/null || echo "no $BRIDGE")"
  vm_running || return 0
  echo "router:  $(rssh test -f /etc/testlab-provisioned 2>/dev/null && echo provisioned || echo 'not provisioned')"
  echo
  fakedevs status
}

cmd_reset() {
  cmd_stop
  log "deleting VM disk"
  rm -f "$DISK" "$SERIAL"
}

cmd_destroy() {
  ip link show "$BRIDGE" >/dev/null 2>&1 && vm_running && fakedevs cleanup || true
  cmd_reset
  log "removing $BRIDGE and $TAP (sudo)"
  sudo sh -c "
    ip link del '$TAP' 2>/dev/null
    ip link del '$BRIDGE' 2>/dev/null
    command -v iptables >/dev/null && iptables -D FORWARD -i '$BRIDGE' -o '$BRIDGE' -j ACCEPT 2>/dev/null
    rm -rf /run/fakedevs
    true"
  rm -rf "$LAB_DIR"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up)        cmd_up ;;
  test)      cmd_test "$@" ;;
  capture)   cmd_capture ;;
  fakes)     fakedevs "$@" ;;
  status)    cmd_status ;;
  ssh)       rssh "$@" ;;
  console)   tail -n 200 -f "$SERIAL" ;;
  env)       live_env "${1:-http}" | sed 's/^/export /' ;;
  stop)      cmd_stop ;;
  reset)     cmd_reset ;;
  destroy)   cmd_destroy ;;
  ""|-h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command: $cmd (see lab.sh --help)" ;;
esac
