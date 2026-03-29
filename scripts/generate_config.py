#!/usr/bin/env python3
"""Сборка config/config.json и config/tun_excluded_routes.env из .env и окружения процесса (без правок сети хоста).

Источник outbound: подписка (URL/файл + SUBSCRIPTION_INDEX) | FULL_STRING | legacy-поля.
TUN_EXCLUDED: авто (текущий узел ± peer по маркерам) или TUN_EXCLUDED_ROUTES; + VPN_LAN_SUBNET.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, unquote, urldefrag, urlparse


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        print(f"Нет файла {path}", file=sys.stderr)
        sys.exit(1)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    return out


def merge_process_env(base: dict[str, str]) -> dict[str, str]:
    """Непустые переменные окружения процесса (Docker Compose environment / -e) перекрывают .env."""
    merged = dict(base)
    for key, value in os.environ.items():
        if value != "":
            merged[key] = value
    return merged


def _b64_decode_subscription_payload(text: str) -> str:
    raw = text.strip()
    if not raw:
        return ""
    pad = (-len(raw)) % 4
    if pad:
        raw += "=" * pad
    try:
        return base64.b64decode(raw, validate=False).decode("utf-8", errors="replace")
    except Exception:
        return text


def fetch_url(url: str, insecure_tls: bool, user_agent: str, timeout_sec: int = 60) -> bytes:
    ctx = ssl.create_default_context()
    if insecure_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent or "curl/8.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout_sec) as resp:
        return resp.read()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def subscription_mode(env: dict[str, str]) -> bool:
    if env.get("SUBSCRIPTION_CACHE_FILE", "").strip():
        return True
    if env.get("SUBSCRIPTION_BODY_FILE", "").strip():
        return True
    return bool(env.get("SUBSCRIPTION_URL", "").strip())


def _subscription_local_path(env: dict[str, str]) -> Path | None:
    for key in ("SUBSCRIPTION_CACHE_FILE", "SUBSCRIPTION_BODY_FILE"):
        raw = env.get(key, "").strip()
        if not raw:
            continue
        root = _project_root()
        p = Path(raw) if raw.startswith("/") else (root / raw)
        return p.resolve()
    return None


def fetch_subscription_body(env: dict[str, str]) -> str:
    local = _subscription_local_path(env)
    if local is not None:
        if not local.is_file():
            print(f"Файл подписки не найден: {local}", file=sys.stderr)
            sys.exit(1)
        print(f"Подписка из файла: {local}", file=sys.stderr, flush=True)
        return local.read_text(encoding="utf-8", errors="replace")

    sub_url = env.get("SUBSCRIPTION_URL", "").strip()
    if not sub_url:
        print("Задайте SUBSCRIPTION_URL или SUBSCRIPTION_CACHE_FILE.", file=sys.stderr)
        sys.exit(1)

    insecure = env.get("SUBSCRIPTION_INSECURE_TLS", "0").strip().lower() in ("1", "true", "yes")
    ua = env.get("SUBSCRIPTION_USER_AGENT", "").strip()
    try:
        timeout_sec = max(5, int(env.get("SUBSCRIPTION_FETCH_TIMEOUT", "45").strip() or "45"))
    except ValueError:
        timeout_sec = 45
    try:
        retries = max(1, int(env.get("SUBSCRIPTION_FETCH_RETRIES", "3").strip() or "3"))
    except ValueError:
        retries = 3

    print(
        f"Загрузка подписки по сети (таймаут {timeout_sec}s, попыток до {retries})…",
        file=sys.stderr,
        flush=True,
    )
    last_err: BaseException | None = None
    for attempt in range(1, retries + 1):
        if attempt > 1:
            wait_s = min(2 * attempt, 15)
            print(f"Повтор {attempt}/{retries} через {wait_s}s…", file=sys.stderr, flush=True)
            time.sleep(wait_s)
        try:
            return fetch_url(sub_url, insecure, ua, timeout_sec=timeout_sec).decode(
                "utf-8", errors="replace"
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            print(f"Попытка {attempt}: {e}", file=sys.stderr, flush=True)
    print(
        "Не удалось скачать подписку. Обход: сохраните ответ панели в файл и задайте "
        "SUBSCRIPTION_CACHE_FILE=config/subscription.cache (см. .env.example).",
        file=sys.stderr,
    )
    if last_err is not None:
        raise last_err
    sys.exit(1)


def _vless_lines_from_text(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vless://"):
            lines.append(line)
    return lines


def extract_vless_lines_from_subscription(body: str) -> list[str]:
    raw = body.strip()
    decoded = _b64_decode_subscription_payload(raw)
    lines = _vless_lines_from_text(decoded)
    if lines:
        return lines
    return _vless_lines_from_text(raw)


def pick_vless_from_subscription_data(data: str, index_one_based: int) -> str:
    lines = extract_vless_lines_from_subscription(data)
    if not lines:
        print("В подписке не найдено строк vless:// после декодирования.", file=sys.stderr)
        sys.exit(1)
    if index_one_based < 1 or index_one_based > len(lines):
        print(
            f"SUBSCRIPTION_INDEX={index_one_based} вне диапазона 1..{len(lines)}, "
            f"используется 1",
            file=sys.stderr,
        )
        index_one_based = 1
    return lines[index_one_based - 1]


def print_subscription_profiles_list(data: str) -> None:
    """Краткий список профилей подписки (хост из vless://) для логов config-keeper."""
    lines = extract_vless_lines_from_subscription(data)
    if not lines:
        return
    for i, line in enumerate(lines, 1):
        host = vless_uri_host(line) or "?"
        print(f"{i}: {host}")


def _qs_first(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = qs.get(key)
    if not vals or not vals[0]:
        return default
    return unquote(vals[0].strip())


def parse_vless_uri(uri: str) -> dict:
    u = urlparse(uri.strip())
    if u.scheme != "vless":
        print("Ожидается схема vless://", file=sys.stderr)
        sys.exit(1)
    netloc = u.netloc
    if "@" not in netloc:
        print("Некорректная vless ссылка (нет @ в netloc).", file=sys.stderr)
        sys.exit(1)
    user_id, host_port = netloc.split("@", 1)
    user_id = user_id.strip()
    host_port = host_port.strip()
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            print(f"Некорректный порт: {port_s}", file=sys.stderr)
            sys.exit(1)
    else:
        host = host_port
        port = 443

    qs = parse_qs(u.query, keep_blank_values=True)
    net_type = _qs_first(qs, "type", "tcp") or "tcp"
    encryption = _qs_first(qs, "encryption", "") or "none"
    fp = _qs_first(qs, "fp", "") or "chrome"
    sni = _qs_first(qs, "sni", "")
    pbk = _qs_first(qs, "pbk", "")
    sid = _qs_first(qs, "sid", "")
    flow = _qs_first(qs, "flow", "")
    spx = _qs_first(qs, "spx", "") or "/"
    pqv = _qs_first(qs, "pqv", "")
    path = _qs_first(qs, "path", "") or "/"
    xhttp_host = _qs_first(qs, "host", "")
    xhttp_mode = _qs_first(qs, "mode", "")

    return {
        "address": host,
        "port": port,
        "id": user_id,
        "encryption": encryption,
        "type": net_type,
        "fp": fp,
        "sni": sni,
        "pbk": pbk,
        "sid": sid,
        "flow": flow,
        "spx": spx,
        "pqv": pqv,
        "path": path,
        "xhttp_host": xhttp_host,
        "xhttp_mode": xhttp_mode,
    }


def vless_uri_host(uri: str) -> str | None:
    u = urlparse(uri.strip())
    if u.scheme != "vless" or "@" not in u.netloc:
        return None
    _uid, host_port = u.netloc.split("@", 1)
    host_port = host_port.strip()
    if ":" in host_port:
        return host_port.rsplit(":", 1)[0].strip()
    return host_port


def vless_uri_quick_summary(uri: str) -> str:
    u = urlparse(uri.strip())
    if u.scheme != "vless" or "@" not in u.netloc:
        return "(не vless)"
    qs = parse_qs(u.query, keep_blank_values=True)
    host = vless_uri_host(uri) or "?"
    net_type = _qs_first(qs, "type", "tcp") or "tcp"
    security = _qs_first(qs, "security", "")
    flow = _qs_first(qs, "flow", "")
    bits = [f"address={host}", f"network={net_type}"]
    if security:
        bits.append(f"security={security}")
    if flow:
        bits.append(f"flow={flow}")
    return ", ".join(bits)


def load_subscription_vless_lines(
    env: dict[str, str],
    subscription_data: str | None = None,
) -> list[str] | None:
    if not subscription_mode(env):
        return None
    if subscription_data is not None:
        return extract_vless_lines_from_subscription(subscription_data)
    data = fetch_subscription_body(env)
    return extract_vless_lines_from_subscription(data)


def excluded_route_markers_from_env(env: dict[str, str]) -> list[str]:
    raw = env.get("TUN_EXCLUDED_ROUTE_MARKERS", "nl1,de1").strip()
    if not raw:
        return ["nl1", "de1"]
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def tun_excluded_include_peer_subscription_hosts(env: dict[str, str]) -> bool:
    raw = env.get("TUN_EXCLUDED_INCLUDE_PEER_HOSTS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def host_matches_excluded_markers(host: str, markers: list[str]) -> bool:
    h = host.strip().lower()
    return any(m in h for m in markers)


def is_literal_ipv4(host: str) -> bool:
    return bool(re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host.strip()))


def resolve_host_to_ipv4s(host: str) -> list[str]:
    """Только IPv4 (/32 в исключениях)."""
    h = host.strip()
    if is_literal_ipv4(h):
        return [h]
    ips: list[str] = []
    try:
        infos = socket.getaddrinfo(h, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as e:
        print(f"DNS TUN_EXCLUDED: не разрешить {host!r}: {e}", file=sys.stderr)
        return ips
    seen: set[str] = set()
    for _fam, _ty, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def build_tun_excluded_routes_value(env: dict[str, str], params: dict, sub_lines: list[str] | None) -> str:
    """CIDR через запятую. Ручной TUN_EXCLUDED_ROUTES иначе авто: outbound + опц. peer (маркеры, см. .env.example)."""
    manual = env.get("TUN_EXCLUDED_ROUTES", "").strip()
    if manual:
        return manual

    markers = excluded_route_markers_from_env(env)
    hosts: set[str] = set()

    cur = (params.get("address") or "").strip()
    if cur:
        hosts.add(cur)

    if sub_lines and tun_excluded_include_peer_subscription_hosts(env):
        for line in sub_lines:
            h = vless_uri_host(line)
            if not h:
                continue
            if host_matches_excluded_markers(h, markers):
                hosts.add(h)

    cidrs: list[str] = []
    seen_ip: set[str] = set()
    for h in sorted(hosts):
        for ip in resolve_host_to_ipv4s(h):
            if ip in seen_ip:
                continue
            seen_ip.add(ip)
            cidrs.append(f"{ip}/32")

    return ",".join(cidrs)


def merge_vpn_lan_subnet_excluded(env: dict[str, str], te_val: str) -> str:
    """Дописывает VPN_LAN_SUBNET в список исключений для маршрута до LAN/шлюза."""
    raw = env.get("VPN_LAN_SUBNET", "").strip()
    if not raw:
        return te_val
    if "/" not in raw:
        print(
            f"VPN_LAN_SUBNET: ожидается CIDR (10.11.12.0/24), пропуск: {raw!r}",
            file=sys.stderr,
        )
        return te_val
    parts = [p.strip() for p in te_val.split(",") if p.strip()]
    if raw not in parts:
        parts.append(raw)
    return ",".join(parts)


def ensure_tun_excluded_non_empty(env: dict[str, str], params: dict, te_val: str) -> None:
    """В авто-режиме без ручного списка требуем хотя бы один IPv4 после резолва."""
    if env.get("TUN_EXCLUDED_ROUTES", "").strip():
        return
    if te_val.strip():
        return
    addr = (params.get("address") or "").strip()
    if not addr:
        return
    if is_literal_ipv4(addr):
        return
    print(
        f"Ошибка: для outbound «{addr}» не получено ни одного IPv4 (DNS?). "
        "Задайте TUN_EXCLUDED_ROUTES=.../32 в .env или проверьте резолв (dig {addr}).",
        file=sys.stderr,
    )
    sys.exit(1)


def write_tun_excluded_routes_env(root: Path, value: str) -> Path:
    path = root / "config" / "tun_excluded_routes.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"# generate_config.py\nTUN_EXCLUDED_ROUTES={value}\n"
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o644)
    except OSError:
        pass
    return path


def params_from_legacy_env(env: dict[str, str]) -> dict:
    def req(name: str) -> str:
        v = env.get(name, "").strip()
        if not v:
            print(f"В .env не задано обязательное поле: {name}", file=sys.stderr)
            sys.exit(1)
        return v

    has_core = (
        env.get("SERVER_ADDRESS", "").strip()
        and env.get("SERVER_PORT", "").strip()
        and env.get("ID", "").strip()
    )
    if not has_core:
        print(
            "Задайте SUBSCRIPTION_URL, или FULL_STRING, или SERVER_ADDRESS + SERVER_PORT + ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    return {
        "address": req("SERVER_ADDRESS"),
        "port": int(req("SERVER_PORT")),
        "id": req("ID"),
        "encryption": env.get("ENCRYPTION", "none").strip() or "none",
        "type": env.get("TYPE", "tcp").strip() or "tcp",
        "fp": req("FP"),
        "sni": req("SNI"),
        "pbk": req("PBK"),
        "sid": req("SID"),
        "flow": env.get("FLOW", "").strip(),
        "spx": env.get("SPX", "/").strip() or "/",
        "pqv": env.get("PQV", "").strip(),
        "path": "/",
        "xhttp_host": "",
        "xhttp_mode": "",
    }


def _socks_listen_address(env: dict[str, str]) -> str:
    """127.0.0.1 или VPN_CONTAINER_IP / XRAY_SOCKS_LISTEN (macvlan + fwmark)."""
    explicit = env.get("XRAY_SOCKS_LISTEN", "").strip()
    if explicit:
        return explicit
    vpn_ip = env.get("VPN_CONTAINER_IP", "").strip()
    if vpn_ip:
        return vpn_ip
    return "127.0.0.1"


def build_xray_config(params: dict, env: dict[str, str]) -> dict:
    socks_port = int(env.get("XRAY_SOCKS_PORT", "10800").strip() or "10800")
    socks_listen = _socks_listen_address(env)
    log_level = env.get("XRAY_LOG_LEVEL", "warning").strip() or "warning"
    net_type = params["type"]

    sid = (params.get("sid") or "").strip()
    reality: dict = {
        "fingerprint": params["fp"],
        "serverName": params["sni"],
        "publicKey": params["pbk"],
        "spx": params["spx"],
    }
    if sid:
        reality["shortId"] = sid
    if params.get("pqv"):
        reality["pqv"] = params["pqv"]

    user: dict = {
        "id": params["id"],
        "encryption": params["encryption"],
    }
    flow = (params.get("flow") or "").strip()
    if net_type == "tcp" and flow:
        user["flow"] = flow

    stream_settings: dict = {
        "network": net_type,
        "security": "reality",
        "realitySettings": reality,
    }
    if net_type == "xhttp":
        xhttp: dict = {"path": params.get("path") or "/"}
        if params.get("xhttp_host"):
            xhttp["host"] = params["xhttp_host"]
        if params.get("xhttp_mode"):
            xhttp["mode"] = params["xhttp_mode"]
        stream_settings["xhttpSettings"] = xhttp

    return {
        "log": {"loglevel": log_level},
        "inbounds": [
            {
                "tag": "socks-in",
                "port": socks_port,
                "listen": socks_listen,
                "protocol": "socks",
                "settings": {"udp": True},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                },
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "tag": "vless-reality",
                "settings": {
                    "vnext": [
                        {
                            "address": params["address"],
                            "port": params["port"],
                            "users": [user],
                        }
                    ]
                },
                "streamSettings": stream_settings,
            }
        ],
        "routing": {"domainStrategy": "AsIs", "rules": []},
    }


def resolve_params(env: dict[str, str], subscription_data: str | None = None) -> dict:
    full_string = env.get("FULL_STRING", "").strip()

    if subscription_mode(env):
        idx = int(env.get("SUBSCRIPTION_INDEX", "1").strip() or "1")
        data = subscription_data if subscription_data is not None else fetch_subscription_body(env)
        uri = pick_vless_from_subscription_data(data, idx)
        sub_url = env.get("SUBSCRIPTION_URL", "").strip()
        cache = env.get("SUBSCRIPTION_CACHE_FILE", "").strip() or env.get("SUBSCRIPTION_BODY_FILE", "").strip()
        if cache:
            print(f"Источник: подписка (файл), индекс {idx}")
        elif sub_url:
            print(f"Источник: подписка (URL), индекс {idx} из {sub_url[:48]}…")
        else:
            print(f"Источник: подписка, индекс {idx}")
        print_subscription_profiles_list(data)
        return parse_vless_uri(uri)

    if full_string:
        print("Источник: FULL_STRING")
        return parse_vless_uri(full_string)

    print("Источник: отдельные поля .env")
    return params_from_legacy_env(env)


def print_parsed_subscription(env: dict[str, str], subscription_data: str | None = None) -> None:
    """Режим --show-parse: stdout, без записи файлов."""
    full_string = env.get("FULL_STRING", "").strip()
    sub_lines: list[str] | None = None

    if subscription_mode(env):
        idx = int(env.get("SUBSCRIPTION_INDEX", "1").strip() or "1")
        data = subscription_data if subscription_data is not None else fetch_subscription_body(env)
        print("=== Подписка ===")
        cache = env.get("SUBSCRIPTION_CACHE_FILE", "").strip() or env.get("SUBSCRIPTION_BODY_FILE", "").strip()
        if cache:
            print(f"SUBSCRIPTION_CACHE_FILE: {cache}")
        sub_url = env.get("SUBSCRIPTION_URL", "").strip()
        if sub_url:
            print(f"SUBSCRIPTION_URL: {sub_url}")
        print(f"SUBSCRIPTION_INDEX: {idx}")
        lines = extract_vless_lines_from_subscription(data)
        sub_lines = lines
        print(f"Строк vless:// после декода: {len(lines)}")
        for i, line in enumerate(lines, 1):
            bare, frag = urldefrag(line)
            mark = "  <-- выбран" if i == idx else ""
            tail = f"  # {frag[:60]}…" if len(frag) > 60 else (f"  # {frag}" if frag else "")
            print(f"  {i}. {bare}{mark}{tail}")
            print(f"      → {vless_uri_quick_summary(line)}")
        if idx < 1 or idx > len(lines):
            print(
                f"SUBSCRIPTION_INDEX={idx} вне диапазона 1..{len(lines)}, используется 1",
                file=sys.stderr,
            )
            idx = 1
        uri = lines[idx - 1]
        params = parse_vless_uri(uri)
    elif full_string:
        print("=== FULL_STRING ===")
        bare, frag = urldefrag(full_string.strip())
        print(f"URI: {bare}")
        if frag:
            print(f"#fragment: {frag}")
        params = parse_vless_uri(full_string.strip())
    else:
        print("=== Поля из .env (без URI) ===")
        params = params_from_legacy_env(env)

    print("\n=== Разобранные поля (как для Xray) ===")
    print(json.dumps(params, indent=2, ensure_ascii=False))

    print("\n=== Фрагмент итогового outbound (streamSettings) ===")
    cfg = build_xray_config(params, env)
    out = cfg["outbounds"][0]
    print(json.dumps({"streamSettings": out["streamSettings"], "users": out["settings"]["vnext"][0]["users"]}, indent=2, ensure_ascii=False))

    te = merge_vpn_lan_subnet_excluded(
        env, build_tun_excluded_routes_value(env, params, sub_lines)
    )
    print("\n=== TUN_EXCLUDED_ROUTES (превью; без --show-parse пишется в config/tun_excluded_routes.env) ===")
    print(te if te else "(пусто — задайте TUN_EXCLUDED_ROUTES в .env или проверьте DNS/маркеры)")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Сборка config/config.json для Xray из .env")
    parser.add_argument(
        "--show-parse",
        action="store_true",
        help="Только вывод в терминал; не пишет config.json и config/tun_excluded_routes.env",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    out_path = root / "config" / "config.json"
    env = merge_process_env(load_dotenv(env_path))

    subscription_data: str | None = None
    try:
        if subscription_mode(env):
            subscription_data = fetch_subscription_body(env)
        if args.show_parse:
            print_parsed_subscription(env, subscription_data)
            return
        params = resolve_params(env, subscription_data)
    except urllib.error.URLError as e:
        print(f"Ошибка загрузки подписки: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Ошибка при загрузке подписки: {e}", file=sys.stderr)
        sys.exit(1)

    sub_lines = load_subscription_vless_lines(env, subscription_data)
    te_val = merge_vpn_lan_subnet_excluded(
        env, build_tun_excluded_routes_value(env, params, sub_lines)
    )
    ensure_tun_excluded_non_empty(env, params, te_val)
    idx_one = env.get("SUBSCRIPTION_INDEX", "1").strip() or "1"
    print(
        f"Outbound (SUBSCRIPTION_INDEX={idx_one}): {params.get('address')} — "
        f"TUN_EXCLUDED_ROUTES={te_val or '(нет)'}"
    )

    cfg = build_xray_config(params, env)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        out_path.chmod(0o644)
    except OSError:
        pass
    print(f"Записано: {out_path}")

    te_path = write_tun_excluded_routes_env(root, te_val)
    print(f"Записано: {te_path}")


if __name__ == "__main__":
    main()
