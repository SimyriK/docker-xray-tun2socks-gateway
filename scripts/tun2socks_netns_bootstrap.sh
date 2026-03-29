#!/bin/sh
# Исключения TUN_EXCLUDED: MARK + ip rule lookup main (исходящий Xray к узлу VPN не в tun).
# При docker restart namespace сохраняется — старые правила чистятся перед добавлением новых.

FWMARK_VAL="${FWMARK:-0x22b}"

sysctl -w net.ipv4.conf.all.rp_filter=0 2>/dev/null || true
sysctl -w net.ipv4.conf.default.rp_filter=0 2>/dev/null || true
iptables -P FORWARD ACCEPT 2>/dev/null || true

# Очистка ip rules priority 50 и mangle OUTPUT от предыдущего запуска
while ip rule del pref 50 2>/dev/null; do :; done
iptables -t mangle -F OUTPUT 2>/dev/null || true

echo "tun2socks_netns_bootstrap: TUN_EXCLUDED_ROUTES=${TUN_EXCLUDED_ROUTES:-}"

if [ -z "${TUN_EXCLUDED_ROUTES}" ]; then
  exit 0
fi

to_cidr() {
  _raw="$1"
  case "${_raw}" in
    */*) echo "${_raw}" ;;
    *) echo "${_raw}/32" ;;
  esac
}

echo "${TUN_EXCLUDED_ROUTES}" | tr ',' '\n' | while IFS= read -r __line; do
  __line=$(echo "${__line}" | tr -d ' ')
  if [ -z "${__line}" ]; then
    continue
  fi
  __cidr="$(to_cidr "${__line}")"
  __host="${__cidr%%/*}"

  if iptables -t mangle -C OUTPUT -d "${__cidr}" -j MARK --set-xmark "${FWMARK_VAL}" 2>/dev/null; then
    :
  elif iptables -t mangle -A OUTPUT -d "${__cidr}" -j MARK --set-xmark "${FWMARK_VAL}" 2>/dev/null; then
    :
  elif iptables -t mangle -C OUTPUT -d "${__host}" -j MARK --set-xmark "${FWMARK_VAL}" 2>/dev/null; then
    :
  elif ! iptables -t mangle -A OUTPUT -d "${__host}" -j MARK --set-xmark "${FWMARK_VAL}" 2>/dev/null; then
    echo "tun2socks_netns_bootstrap: iptables mangle MARK не применён для ${__cidr}" >&2
  fi

  if ! ip rule add pref 50 from all to "${__cidr}" lookup main 2>/dev/null; then
    if ! ip rule add pref 50 to "${__cidr}" lookup main 2>/dev/null; then
      echo "tun2socks_netns_bootstrap: ip rule lookup main не добавлен для ${__cidr}" >&2
    fi
  fi
done
