#!/usr/bin/env sh
# Периодическое обновление config.json из подписки и перезапуск контейнера gateway при изменении.
# Также запускает WebUI (webui.py) в фоне.

set -eu

readyMarker="/tmp/config-keeper-ready"
hours="${SUBSCRIPTION_UPDATE_INTERVAL:-24}"
gatewayName="${GATEWAY_CONTAINER_NAME:-xray-gateway}"
cd "$(dirname "$0")/.."

if command -v apk >/dev/null 2>&1; then
  apk add --no-cache docker-cli >/dev/null 2>&1 || true
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Нужен python3" >&2
  exit 1
fi

python3 scripts/webui.py &
echo "WebUI запущен (PID $!) на порту ${WEBUI_PORT:-8080}"

restart_gateway() {
  if command -v docker >/dev/null 2>&1; then
    docker restart "${gatewayName}" >/dev/null 2>&1 || true
  fi
}

refresh_subscription_env() {
  _val="$(grep '^SUBSCRIPTION_URL=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
  if [ -n "${_val}" ]; then export SUBSCRIPTION_URL="${_val}"; fi
  _val="$(grep '^SUBSCRIPTION_INDEX=' .env 2>/dev/null | head -1 | cut -d= -f2-)"
  if [ -n "${_val}" ]; then export SUBSCRIPTION_INDEX="${_val}"; fi
}

while true; do
  oldHash=""
  if [ -f config/config.json ]; then
    oldHash="$(sha256sum config/config.json | awk '{print $1}')"
  fi
  refresh_subscription_env
  if [ "${SKIP_COMPOSE_RENDER:-0}" = "1" ] && [ ! -f "${readyMarker}" ]; then
    echo "SKIP_COMPOSE_RENDER=1 — стартовая генерация пропущена"
  else
    if ! python3 scripts/generate_config.py; then
      echo "$(date -Iseconds 2>/dev/null || date) ошибка generate_config.py, следующая попытка через ${hours} ч" >&2
      sleep $((hours * 3600))
      continue
    fi
  fi
  if [ ! -f "${readyMarker}" ]; then
    touch "${readyMarker}"
  fi
  if [ ! -f config/config.json ]; then
    echo "$(date -Iseconds 2>/dev/null || date) нет config/config.json после генерации" >&2
    sleep $((hours * 3600))
    continue
  fi
  newHash="$(sha256sum config/config.json | awk '{print $1}')"
  if [ "${oldHash}" != "${newHash}" ]; then
    echo "$(date -Iseconds 2>/dev/null || date) config.json изменён, перезапуск ${gatewayName}"
    restart_gateway
  else
    echo "$(date -Iseconds 2>/dev/null || date) подписка без изменений"
  fi
  sleep $((hours * 3600))
done
