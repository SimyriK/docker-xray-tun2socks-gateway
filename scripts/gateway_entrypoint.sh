#!/bin/sh
set -eu

sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true
sysctl -w net.ipv4.conf.all.rp_filter=0 2>/dev/null || true
sysctl -w net.ipv4.conf.default.rp_filter=0 2>/dev/null || true

# env_file читается только при create; при docker restart файл мог измениться — перечитываем
if [ -f /etc/tun_excluded_routes.env ]; then
  set +e; . /etc/tun_excluded_routes.env; set -e
  export TUN_EXCLUDED_ROUTES
  echo "TUN_EXCLUDED_ROUTES=${TUN_EXCLUDED_ROUTES:-}"
fi

xray run -config "${XRAY_CONFIG:-/etc/xray/config.json}" &
xrayPid=$!
echo "xray запущен (PID ${xrayPid})"
sleep 2

if [ -f /tun2socks_netns_bootstrap.sh ]; then
  export EXTRA_COMMANDS="/bin/sh /tun2socks_netns_bootstrap.sh"
fi

(while kill -0 "${xrayPid}" 2>/dev/null; do sleep 10; done; echo "xray завершился, остановка контейнера" >&2; kill 1 2>/dev/null) &

exec /entrypoint.sh
