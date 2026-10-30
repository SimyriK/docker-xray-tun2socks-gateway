#!/usr/bin/env python3
"""Проверки доступности: TCP/TLS до нод и path-health через gateway."""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


DEFAULT_PATH_URL = "http://www.gstatic.com/generate_204"
DEFAULT_PATH_INTERVAL = 60
DEFAULT_PROFILE_INTERVAL = 300
DEFAULT_PROBE_TIMEOUT = 5.0


class HealthState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pathHealth: dict[str, Any] | None = None
        self.profileProbes: dict[int, dict[str, Any]] = {}
        self.versions: dict[str, Any] | None = None
        self.lastPathAt = 0.0
        self.lastProfilesAt = 0.0
        self.probingPath = False
        self.probingProfiles = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "pathHealth": self.pathHealth,
                "profileProbes": dict(self.profileProbes),
                "versions": self.versions,
                "probingPath": self.probingPath,
                "probingProfiles": self.probingProfiles,
                "lastPathAt": self.lastPathAt,
                "lastProfilesAt": self.lastProfilesAt,
            }


STATE = HealthState()


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def probe_tcp_tls(
    host: str,
    port: int = 443,
    timeout: float | None = None,
    serverName: str | None = None,
) -> dict[str, Any]:
    """TCP connect + TLS handshake (как «пинг» в клиентах, без полного VLESS)."""
    timeout = DEFAULT_PROBE_TIMEOUT if timeout is None else timeout
    sni = serverName or host
    result: dict[str, Any] = {
        "host": host,
        "port": port,
        "ok": False,
        "tcpMs": None,
        "tlsMs": None,
        "error": None,
    }
    sock: socket.socket | None = None
    try:
        t0 = time.perf_counter()
        sock = socket.create_connection((host, port), timeout=timeout)
        result["tcpMs"] = round((time.perf_counter() - t0) * 1000)
        ctx = ssl.create_default_context()
        # Reality/SNI часто не совпадает с публичным сертификатом
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        t1 = time.perf_counter()
        with ctx.wrap_socket(sock, server_hostname=sni) as tlsSock:
            tlsSock.do_handshake()
            result["tlsMs"] = round((time.perf_counter() - t1) * 1000)
        sock = None
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return result


def probe_profiles(
    profiles: list[dict[str, Any]],
    timeout: float | None = None,
    workers: int = 8,
) -> dict[int, dict[str, Any]]:
    timeout = DEFAULT_PROBE_TIMEOUT if timeout is None else timeout
    out: dict[int, dict[str, Any]] = {}
    if not profiles:
        return out

    def _one(profile: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        idx = int(profile["index"])
        host = str(profile.get("host") or "")
        port = int(profile.get("port") or 443)
        sni = profile.get("sni") or host
        if not host or host == "?":
            return idx, {
                "index": idx,
                "host": host,
                "ok": False,
                "tcpMs": None,
                "tlsMs": None,
                "error": "нет host",
                "checkedAt": time.time(),
            }
        probe = probe_tcp_tls(host, port, timeout=timeout, serverName=str(sni))
        probe["index"] = idx
        probe["checkedAt"] = time.time()
        return idx, probe

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(profiles)))) as pool:
        futures = [pool.submit(_one, p) for p in profiles]
        for fut in as_completed(futures):
            idx, probe = fut.result()
            out[idx] = probe
    return out


def _parse_curl_write_out(raw: str) -> tuple[int | None, float | None]:
    text = (raw or "").strip()
    if not text:
        return None, None
    parts = text.split()
    code: int | None = None
    seconds: float | None = None
    if parts:
        try:
            code = int(parts[0])
        except ValueError:
            code = None
    if len(parts) >= 2:
        try:
            seconds = float(parts[1])
        except ValueError:
            seconds = None
    return code, seconds


def probe_path_via_gateway(
    gatewayName: str | None = None,
    url: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """HTTP generate_204 из network namespace gateway (реальный путь через TUN/VPN)."""
    gatewayName = gatewayName or os.environ.get("GATEWAY_CONTAINER_NAME", "xray-gateway")
    url = url or os.environ.get("HEALTH_PATH_URL", DEFAULT_PATH_URL).strip() or DEFAULT_PATH_URL
    timeout = (
        _env_float("HEALTH_PATH_TIMEOUT", DEFAULT_PROBE_TIMEOUT)
        if timeout is None
        else timeout
    )
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "ms": None,
        "httpCode": None,
        "error": None,
        "checkedAt": time.time(),
    }
    maxTime = max(1, int(timeout))
    cmd = [
        "docker",
        "exec",
        gatewayName,
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{time_starttransfer}",
        "--connect-timeout",
        str(maxTime),
        "--max-time",
        str(maxTime),
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=maxTime + 3,
        )
    except FileNotFoundError:
        result["error"] = "docker не найден"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = f"таймаут {maxTime}s"
        return result

    code, seconds = _parse_curl_write_out(proc.stdout)
    result["httpCode"] = code
    if seconds is not None:
        result["ms"] = round(seconds * 1000)
    # generate_204 → 204; некоторые зеркала отдают 200
    if proc.returncode == 0 and code in (200, 204):
        result["ok"] = True
    else:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        if not err:
            err = f"curl exit={proc.returncode} http={code}"
        result["error"] = err[-300:]
    return result


def _first_version_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def fetch_versions(gatewayName: str | None = None) -> dict[str, Any]:
    gatewayName = gatewayName or os.environ.get("GATEWAY_CONTAINER_NAME", "xray-gateway")
    out: dict[str, Any] = {
        "xray": None,
        "tun2socks": None,
        "error": None,
        "checkedAt": time.time(),
    }
    try:
        xray = subprocess.run(
            ["docker", "exec", gatewayName, "xray", "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if xray.returncode == 0:
            out["xray"] = _first_version_line(xray.stdout)
        else:
            out["error"] = (xray.stderr or xray.stdout or "xray version failed")[-200:]

        tunLine = None
        for verArgs in (["--version"], ["-v"], ["-version"]):
            tun = subprocess.run(
                ["docker", "exec", gatewayName, "tun2socks", *verArgs],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if tun.returncode == 0:
                tunLine = _first_version_line(tun.stdout) or _first_version_line(tun.stderr)
                if tunLine:
                    break
        if tunLine:
            out["tun2socks"] = tunLine
        elif not out["error"]:
            out["error"] = "tun2socks version failed"
    except FileNotFoundError:
        out["error"] = "docker не найден"
    except subprocess.TimeoutExpired:
        out["error"] = "таймаут docker exec"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def refresh_versions(force: bool = False) -> dict[str, Any]:
    with STATE.lock:
        age = time.time() - (STATE.versions or {}).get("checkedAt", 0)
        if not force and STATE.versions and age < 300:
            return dict(STATE.versions)
    versions = fetch_versions()
    with STATE.lock:
        STATE.versions = versions
    return versions


def refresh_path_health(force: bool = False) -> dict[str, Any]:
    interval = _env_int("HEALTH_PATH_INTERVAL", DEFAULT_PATH_INTERVAL)
    with STATE.lock:
        if STATE.probingPath:
            return dict(STATE.pathHealth or {})
        if not force and STATE.pathHealth and (time.time() - STATE.lastPathAt) < interval:
            return dict(STATE.pathHealth)
        STATE.probingPath = True
    try:
        result = probe_path_via_gateway()
        with STATE.lock:
            STATE.pathHealth = result
            STATE.lastPathAt = time.time()
        return result
    finally:
        with STATE.lock:
            STATE.probingPath = False


def refresh_profile_probes(
    profiles: list[dict[str, Any]],
    force: bool = False,
) -> dict[int, dict[str, Any]]:
    interval = _env_int("HEALTH_PROFILE_INTERVAL", DEFAULT_PROFILE_INTERVAL)
    with STATE.lock:
        if STATE.probingProfiles:
            return dict(STATE.profileProbes)
        if (
            not force
            and STATE.profileProbes
            and (time.time() - STATE.lastProfilesAt) < interval
        ):
            return dict(STATE.profileProbes)
        STATE.probingProfiles = True
    try:
        probes = probe_profiles(profiles)
        with STATE.lock:
            STATE.profileProbes = probes
            STATE.lastProfilesAt = time.time()
        return probes
    finally:
        with STATE.lock:
            STATE.probingProfiles = False


def start_background(
    profiles_provider,
) -> None:
    """profiles_provider: callable() -> list[dict]"""

    def _loop() -> None:
        # небольшая пауза после старта WebUI / gateway
        time.sleep(8)
        while True:
            try:
                refresh_versions(force=False)
                refresh_path_health(force=False)
                profiles = profiles_provider() or []
                if profiles:
                    refresh_profile_probes(profiles, force=False)
            except Exception as exc:
                print(f"health_probe bg: {exc}", flush=True)
            time.sleep(5)

    thread = threading.Thread(target=_loop, name="health-probe", daemon=True)
    thread.start()


def http_probe_direct(url: str, timeout: float = 5.0) -> dict[str, Any]:
    """Прямой HTTP без SOCKS (вспомогательное)."""
    result: dict[str, Any] = {"ok": False, "ms": None, "httpCode": None, "error": None}
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result["httpCode"] = getattr(resp, "status", None) or resp.getcode()
            result["ms"] = round((time.perf_counter() - t0) * 1000)
            result["ok"] = result["httpCode"] in (200, 204)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["ms"] = round((time.perf_counter() - t0) * 1000)
    return result
