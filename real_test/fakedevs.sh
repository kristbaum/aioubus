#!/usr/bin/env bash
# fakedevs.sh - generate fake wired and Wi-Fi devices against the OpenWrt test VM (see lab.sh)
#
# Wired fakes: network namespaces on the host bridge (br-owrt), with a DHCP lease
#              from OpenWrt and a keepalive ping so they stay in the ARP table.
# Wi-Fi fakes: station interfaces on mac80211_hwsim radios inside the VM,
#              configured via ssh + uci, so they show up in hostapd get_clients.
#
# Usage (run as root, e.g. via sudo):
#   fakedevs.sh setup                    push Wi-Fi station configs to the router
#   fakedevs.sh up   <name|all|wired|wifi>
#   fakedevs.sh down <name|all|wired|wifi> [--hard]
#   fakedevs.sh churn [interval_s] [rounds]   randomly toggle "mobile" devices (rounds 0 = forever)
#   fakedevs.sh wait [timeout_s]         wait until every Wi-Fi fake that is up has associated
#   fakedevs.sh status
#   fakedevs.sh list
#   fakedevs.sh expect <wired|wifi>      print "mac=name,..." of fakes that are up (no root needed)
#   fakedevs.sh cleanup                  remove all fakes and router-side config
#
# Settings come from lab.env (ROUTER, BRIDGE, SSID, PSK, LAB_NET, LAB_DIR);
# SSH_KEY, SSH_PORT and STATIC_NET can be overridden as well.

set -uo pipefail

# shellcheck source=lab.env
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lab.env"
SSH_PORT="${SSH_PORT:-22}"
STATIC_NET="${STATIC_NET:-$LAB_NET}"   # fallback if no DHCP client is available
SSH_KEY="${SSH_KEY:-$LAB_DIR/id_ed25519}"   # created by lab.sh
STATE_DIR="/run/fakedevs"

# ---------------------------------------------------------------------------
# Device table
#   name | medium (wired|wifi) | MAC | role (static|mobile) | description
# Wi-Fi devices are assigned radio1..radio7 in table order (radio0 is the AP).
# MACs starting with 02: are locally administered, like phones with randomized MACs.
# ---------------------------------------------------------------------------
DEVICES=(
  "nas|wired|00:11:32:a1:00:01|static|Synology NAS"
  "hue-bridge|wired|00:17:88:a1:00:02|static|Philips Hue bridge"
  "raspberrypi|wired|dc:a6:32:a1:00:03|static|Raspberry Pi 4"
  "sonos-wired|wired|00:0e:58:a1:00:04|static|Sonos speaker"
  "desktop|wired|02:00:00:a1:00:05|mobile|Desktop PC (sleeps)"
  "iphone|wifi|f0:18:98:b2:00:01|mobile|iPhone (fixed MAC)"
  "android|wifi|02:5a:3c:b2:00:02|mobile|Android phone (randomized MAC)"
  "laptop|wifi|02:00:00:b2:00:03|mobile|Laptop"
  "esp32-sensor|wifi|24:0a:c4:b2:00:04|static|ESP32 sensor"
  "nest-thermostat|wifi|18:b4:30:b2:00:05|static|Nest thermostat"
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
die()  { echo "error: $*" >&2; exit 1; }
log()  { printf '%s  %s\n' "$(date +%T)" "$*"; }

field() { # field <record> <index 1..5>
  echo "$1" | cut -d'|' -f"$2"
}

record_of() {
  local r
  for r in "${DEVICES[@]}"; do
    [[ "$(field "$r" 1)" == "$1" ]] && { echo "$r"; return 0; }
  done
  return 1
}

index_of() { # position of device in table (0-based)
  local i
  for i in "${!DEVICES[@]}"; do
    [[ "$(field "${DEVICES[$i]}" 1)" == "$1" ]] && { echo "$i"; return 0; }
  done
  return 1
}

wifi_radio_of() { # radioN for a wifi device, by order among wifi devices
  local n=0 r
  for r in "${DEVICES[@]}"; do
    [[ "$(field "$r" 2)" == "wifi" ]] || continue
    n=$((n + 1))
    [[ "$(field "$r" 1)" == "$1" ]] && { echo "radio$n"; return 0; }
  done
  return 1
}

uci_name() { echo "fake_${1//-/_}"; }

select_names() { # expand all|wired|wifi|<name>
  local sel="$1" r
  for r in "${DEVICES[@]}"; do
    local n m
    n="$(field "$r" 1)"; m="$(field "$r" 2)"
    case "$sel" in
      all) echo "$n" ;;
      wired|wifi) [[ "$m" == "$sel" ]] && echo "$n" ;;
      *) [[ "$n" == "$sel" ]] && echo "$n" ;;
    esac
  done
}

rssh() {
  local opts=(-p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
              -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes)
  [[ -f "$SSH_KEY" ]] && opts+=(-i "$SSH_KEY")
  ssh "${opts[@]}" "root@$ROUTER" "$@"
}

need_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo $0 ...)"; }

dhcp_client() {
  if command -v busybox >/dev/null && busybox --list | grep -qx udhcpc; then echo udhcpc
  elif command -v dhclient >/dev/null; then echo dhclient
  else echo none
  fi
}

write_udhcpc_script() {
  cat > "$STATE_DIR/udhcpc.sh" <<'EOF'
#!/bin/sh
case "$1" in
  deconfig) ip addr flush dev "$interface" ;;
  bound|renew)
    ip addr flush dev "$interface"
    ip addr add "$ip/${mask:-24}" dev "$interface"
    [ -n "$router" ] && ip route replace default via "${router%% *}" dev "$interface"
    ;;
esac
EOF
  chmod +x "$STATE_DIR/udhcpc.sh"
}

# ---------------------------------------------------------------------------
# wired devices
# ---------------------------------------------------------------------------
wired_up() {
  local name="$1" rec mac idx ns veth client ip
  rec="$(record_of "$name")"; mac="$(field "$rec" 3)"
  idx="$(index_of "$name")"; ns="fd-$name"; veth="fdv$idx"

  if ip netns list | grep -qw "$ns"; then log "$name already up"; return; fi

  ip netns add "$ns"
  ip link add "$veth" type veth peer name eth0 netns "$ns"
  ip link set "$veth" master "$BRIDGE" up
  ip -n "$ns" link set lo up
  ip -n "$ns" link set eth0 address "$mac"
  ip -n "$ns" link set eth0 up

  client="$(dhcp_client)"
  case "$client" in
    udhcpc)
      ip netns exec "$ns" busybox udhcpc -i eth0 -q -n -t 5 \
        -x "hostname:$name" -s "$STATE_DIR/udhcpc.sh" >/dev/null 2>&1 \
        || log "$name: no DHCP lease"
      ;;
    dhclient)
      printf 'send host-name "%s";\n' "$name" > "$STATE_DIR/$name.dhclient.conf"
      ip netns exec "$ns" dhclient -1 -cf "$STATE_DIR/$name.dhclient.conf" \
        -pf "$STATE_DIR/$name.dhclient.pid" -lf "$STATE_DIR/$name.leases" eth0 \
        >/dev/null 2>&1 || log "$name: no DHCP lease"
      ;;
    none)
      ip="$STATIC_NET.$((200 + idx))"
      ip -n "$ns" addr add "$ip/24" dev eth0
      log "$name: no DHCP client on host, using static $ip (ARP only, no lease)"
      ;;
  esac

  # keepalive so the router keeps a REACHABLE neighbour entry
  ip netns exec "$ns" sh -c "while :; do ping -c1 -W1 $ROUTER >/dev/null 2>&1; sleep 5; done" \
    </dev/null >/dev/null 2>&1 &
  echo $! > "$STATE_DIR/$name.pid"

  ip="$(ip -n "$ns" -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1)"
  log "UP    wired  $name  $mac  ${ip:-no-ip}"
}

wired_down() {
  local name="$1" hard="$2"
  local ns="fd-$name" rec mac
  rec="$(record_of "$name")"; mac="$(field "$rec" 3)"

  if [[ -f "$STATE_DIR/$name.pid" ]]; then
    kill "$(cat "$STATE_DIR/$name.pid")" 2>/dev/null
    rm -f "$STATE_DIR/$name.pid"
  fi
  [[ -f "$STATE_DIR/$name.dhclient.pid" ]] && kill "$(cat "$STATE_DIR/$name.dhclient.pid")" 2>/dev/null
  rm -f "$STATE_DIR/$name".dhclient.* "$STATE_DIR/$name.leases"

  if ip netns list | grep -qw "$ns"; then
    ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null
    ip netns del "$ns"     # takes the veth pair with it
  fi

  if [[ "$hard" == 1 ]]; then
    rssh "ip neigh show dev br-lan | grep -i '$mac' | awk '{print \$1}' | \
          while read a; do ip neigh del \$a dev br-lan; done" 2>/dev/null
  fi
  log "DOWN  wired  $name  $mac$([[ $hard == 1 ]] && echo '  (neighbour flushed)')"
}

wired_is_up() { ip netns list | grep -qw "fd-$1"; }

# ---------------------------------------------------------------------------
# Wi-Fi devices (station interfaces on hwsim radios inside the VM)
# ---------------------------------------------------------------------------
wifi_setup() {
  local r name mac radio sec script=""
  for r in "${DEVICES[@]}"; do
    [[ "$(field "$r" 2)" == "wifi" ]] || continue
    name="$(field "$r" 1)"; mac="$(field "$r" 3)"
    radio="$(wifi_radio_of "$name")"; sec="$(uci_name "$name")"
    script+="
uci -q get wireless.$radio >/dev/null || { echo 'missing $radio (load mac80211_hwsim radios=8)'; exit 1; }
uci set wireless.$radio.disabled='1'
uci set wireless.$radio.band='2g'
uci set wireless.$radio.channel='1'
uci -q delete wireless.default_$radio
uci -q delete wireless.$sec
uci set wireless.$sec=wifi-iface
uci set wireless.$sec.device='$radio'
uci set wireless.$sec.mode='sta'
uci set wireless.$sec.ssid='$SSID'
uci set wireless.$sec.encryption='psk2'
uci set wireless.$sec.key='$PSK'
uci set wireless.$sec.macaddr='$mac'
"
  done
  script+="uci commit wireless; wifi"
  rssh "$script" || die "router setup failed"
  log "router: Wi-Fi station configs written (all start disabled)"
}

wifi_up() {
  local name="$1" radio mac
  radio="$(wifi_radio_of "$name")"; mac="$(field "$(record_of "$name")" 3)"
  rssh "uci set wireless.$radio.disabled='0'; uci commit wireless; wifi up $radio" \
    || { log "$name: ssh failed"; return; }
  log "UP    wifi   $name  $mac  ($radio)"
}

wifi_down() {
  local name="$1" hard="$2" radio mac
  radio="$(wifi_radio_of "$name")"; mac="$(field "$(record_of "$name")" 3)"
  local cmd="wifi down $radio; uci set wireless.$radio.disabled='1'; uci commit wireless"
  [[ "$hard" == 1 ]] && cmd+="; ip neigh show dev br-lan | grep -i '$mac' | awk '{print \$1}' | while read a; do ip neigh del \$a dev br-lan; done"
  rssh "$cmd" || { log "$name: ssh failed"; return; }
  log "DOWN  wifi   $name  $mac  ($radio)"
}

wifi_is_up() {
  local radio; radio="$(wifi_radio_of "$1")"
  [[ "$(rssh "uci -q get wireless.$radio.disabled" 2>/dev/null)" == "0" ]]
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
dev_up() {
  case "$(field "$(record_of "$1")" 2)" in
    wired) wired_up "$1" ;; wifi) wifi_up "$1" ;;
  esac
}
dev_down() {
  case "$(field "$(record_of "$1")" 2)" in
    wired) wired_down "$1" "$2" ;; wifi) wifi_down "$1" "$2" ;;
  esac
}
dev_is_up() {
  case "$(field "$(record_of "$1")" 2)" in
    wired) wired_is_up "$1" ;; wifi) wifi_is_up "$1" ;;
  esac
}

cmd_list() {
  printf '%-16s %-6s %-18s %-7s %s\n' NAME MEDIUM MAC ROLE DESCRIPTION
  local r
  for r in "${DEVICES[@]}"; do
    IFS='|' read -r n m mac role desc <<< "$r"
    printf '%-16s %-6s %-18s %-7s %s\n' "$n" "$m" "$mac" "$role" "$desc"
  done
}

cmd_status() {
  echo "== fake devices (host view) =="
  local r n state
  for r in "${DEVICES[@]}"; do
    n="$(field "$r" 1)"
    if dev_is_up "$n"; then state=UP; else state=down; fi
    printf '  %-16s %-6s %-18s %s\n' "$n" "$(field "$r" 2)" "$(field "$r" 3)" "$state"
  done
  echo
  echo "== router: hostapd associations (ubus) =="
  rssh 'for o in $(ubus list "hostapd.*"); do echo "  $o"; ubus call $o get_clients | jsonfilter -e "@.clients" | sed "s/^/    /"; done' \
    || echo "  (ssh failed)"
  echo
  echo "== router: DHCP leases =="
  rssh 'cat /tmp/dhcp.leases' | sed 's/^/  /'
  echo
  echo "== router: neighbours on br-lan =="
  rssh 'ip neigh show dev br-lan' | sed 's/^/  /'
}

associated_macs() { # all station MACs across hostapd objects, one per line
  rssh 'for o in $(ubus list "hostapd.*"); do ubus call $o get_clients | jsonfilter -e "@.clients" ; done' \
    2>/dev/null | grep -oiE '"([0-9a-f]{2}:){5}[0-9a-f]{2}"' | tr -d '"' | tr 'A-F' 'a-f'
}

cmd_wait() {
  local timeout="${1:-60}" deadline r n mac missing
  deadline=$((SECONDS + timeout))
  while :; do
    missing=()
    local assoc; assoc="$(associated_macs)"
    for r in "${DEVICES[@]}"; do
      [[ "$(field "$r" 2)" == "wifi" ]] || continue
      n="$(field "$r" 1)"; mac="$(field "$r" 3)"
      wifi_is_up "$n" || continue
      grep -qx "$mac" <<< "$assoc" || missing+=("$n")
    done
    (( ${#missing[@]} == 0 )) && { log "all Wi-Fi fakes that are up are associated"; return 0; }
    (( SECONDS < deadline )) || die "not associated after ${timeout}s: ${missing[*]}"
    sleep 2
  done
}

cmd_expect() { # mac=name pairs of the fakes of one medium that are currently up
  local medium="$1" r n out=()
  for r in "${DEVICES[@]}"; do
    [[ "$(field "$r" 2)" == "$medium" ]] || continue
    n="$(field "$r" 1)"
    dev_is_up "$n" && out+=("$(field "$r" 3)=$n")
  done
  local IFS=,; echo "${out[*]}"
}

cmd_churn() {
  local interval="${1:-30}" rounds="${2:-0}" i=0 mobiles=() r n
  for r in "${DEVICES[@]}"; do
    [[ "$(field "$r" 4)" == "mobile" ]] && mobiles+=("$(field "$r" 1)")
  done
  (( ${#mobiles[@]} )) || die "no devices with role 'mobile'"
  log "churn: ${#mobiles[@]} mobile devices, every ${interval}s, rounds=${rounds:-forever}"
  trap 'log "churn stopped"; exit 0' INT TERM
  while (( rounds == 0 || i < rounds )); do
    n="${mobiles[RANDOM % ${#mobiles[@]}]}"
    if dev_is_up "$n"; then
      # half of the departures are "hard" so both fast and slow expiry get exercised
      dev_down "$n" $((RANDOM % 2))
    else
      dev_up "$n"
    fi
    i=$((i + 1))
    sleep "$interval"
  done
}

cmd_cleanup() {
  local r n
  for r in "${DEVICES[@]}"; do
    n="$(field "$r" 1)"
    [[ "$(field "$r" 2)" == "wired" ]] && wired_down "$n" 1 >/dev/null
  done
  local script=""
  for r in "${DEVICES[@]}"; do
    [[ "$(field "$r" 2)" == "wifi" ]] || continue
    n="$(field "$r" 1)"
    script+="wifi down $(wifi_radio_of "$n") 2>/dev/null; uci -q delete wireless.$(uci_name "$n"); uci set wireless.$(wifi_radio_of "$n").disabled='1';"
  done
  rssh "$script uci commit wireless; wifi" && log "router: Wi-Fi fakes removed"
  rm -rf "$STATE_DIR"
  log "cleanup done"
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    list) cmd_list; return ;;
    expect)
      [[ "${1:-}" == wired || "${1:-}" == wifi ]] || die "usage: expect <wired|wifi>"
      cmd_expect "$1"; return ;;
    ""|-h|--help) sed -n '2,22p' "$0"; return ;;
  esac

  need_root
  mkdir -p "$STATE_DIR"
  write_udhcpc_script
  ip link show "$BRIDGE" >/dev/null 2>&1 || die "bridge $BRIDGE not found"

  local hard=0 a args=()
  for a in "$@"; do [[ "$a" == "--hard" ]] && hard=1 || args+=("$a"); done

  case "$cmd" in
    setup)   wifi_setup ;;
    up)      for n in $(select_names "${args[0]:-all}"); do dev_up "$n"; done ;;
    down)    for n in $(select_names "${args[0]:-all}"); do dev_down "$n" "$hard"; done ;;
    wait)    cmd_wait "${args[0]:-60}" ;;
    churn)   cmd_churn "${args[0]:-30}" "${args[1]:-0}" ;;
    status)  cmd_status ;;
    cleanup) cmd_cleanup ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
