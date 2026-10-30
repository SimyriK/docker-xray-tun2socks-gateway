#!/usr/bin/env python3
"""Мини-WebUI для выбора профиля подписки Xray gateway."""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_config  # noqa: E402
import health_probe  # noqa: E402


def _read_env_value(key: str) -> str:
    if not ENV_PATH.is_file():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", stripped)
        if m and m.group(1) == key:
            val = m.group(2).strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            return val
    return ""


def _update_env_values(updates: dict[str, str]) -> None:
    """Обновить или раскомментировать ключи в .env; дубликаты удаляются."""
    lines = (
        ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []
    )
    replaced: set[str] = set()
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        matched_key: str | None = None
        for key in updates:
            if re.match(rf"^#?\s*{re.escape(key)}\s*=", stripped):
                matched_key = key
                break

        if matched_key is not None:
            if matched_key not in replaced:
                result.append(f"{matched_key}={updates[matched_key]}")
                replaced.add(matched_key)
        else:
            result.append(line)

    for key, value in updates.items():
        if key not in replaced:
            result.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(result) + "\n", encoding="utf-8")


def _safe_profile_meta(line: str) -> dict:
    host = generate_config.vless_uri_host(line) or "?"
    port = 443
    sni = host
    try:
        u = urlparse(line.strip())
        if "@" in u.netloc:
            _uid, hostPort = u.netloc.split("@", 1)
            hostPort = hostPort.strip()
            if ":" in hostPort:
                _h, portS = hostPort.rsplit(":", 1)
                try:
                    port = int(portS)
                except ValueError:
                    port = 443
        qs = parse_qs(u.query, keep_blank_values=True)
        sniVal = (qs.get("sni") or [""])[0].strip()
        if sniVal:
            sni = sniVal
    except Exception:
        pass
    return {"host": host, "port": port, "sni": sni}


def _get_profiles(url: str) -> list[dict]:
    if not url:
        return []
    try:
        env = generate_config.load_dotenv(ENV_PATH)
    except SystemExit:
        env = {}
    env = generate_config.merge_process_env(env)
    env["SUBSCRIPTION_URL"] = url
    try:
        data = generate_config.fetch_subscription_body(env)
    except (SystemExit, Exception):
        return []
    lines = generate_config.extract_vless_lines_from_subscription(data)
    profiles: list[dict] = []
    for i, line in enumerate(lines, 1):
        meta = _safe_profile_meta(line)
        profiles.append(
            {
                "index": i,
                "host": meta["host"],
                "port": meta["port"],
                "sni": meta["sni"],
            }
        )
    return profiles


def _current_profiles() -> list[dict]:
    url = _read_env_value("SUBSCRIPTION_URL")
    return _get_profiles(url) if url else []


def _enrich_profiles(profiles: list[dict]) -> list[dict]:
    snap = health_probe.STATE.snapshot()
    probes = snap.get("profileProbes") or {}
    out: list[dict] = []
    for profile in profiles:
        item = dict(profile)
        probe = probes.get(int(profile["index"]))
        if probe:
            item["probe"] = probe
        out.append(item)
    return out


def _apply(url: str, index: int) -> dict:
    _update_env_values(
        {"SUBSCRIPTION_URL": url, "SUBSCRIPTION_INDEX": str(index)}
    )

    gateway_name = os.environ.get("GATEWAY_CONTAINER_NAME", "xray-gateway")

    env = os.environ.copy()
    env["SUBSCRIPTION_URL"] = url
    env["SUBSCRIPTION_INDEX"] = str(index)

    try:
        gen = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_config.py")],
            env=env,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "generate_config.py: таймаут 120 с"}

    if gen.returncode != 0:
        return {
            "ok": False,
            "error": f"generate_config.py завершился с кодом {gen.returncode}",
            "detail": (gen.stderr or gen.stdout or "")[-500:],
        }

    try:
        rst = subprocess.run(
            ["docker", "restart", gateway_name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        restarted = rst.returncode == 0
        restart_err = rst.stderr.strip() if not restarted else ""
    except FileNotFoundError:
        restarted = False
        restart_err = "docker не найден"
    except subprocess.TimeoutExpired:
        restarted = False
        restart_err = "docker restart: таймаут 60 с"

    return {
        "ok": True,
        "restarted": restarted,
        "output": (gen.stdout or "")[-500:],
        "restartError": restart_err,
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Xray Gateway</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f5f6fa;--fg:#1a1a2e;--card:#fff;--border:#e2e4ea;
  --accent:#4361ee;--accent-h:#3a56d4;--accent-l:#eef1ff;
  --ok:#2d6a4f;--ok-bg:#d8f3dc;--err:#c1121f;--err-bg:#fde8e8;
  --warn:#9a6700;--warn-bg:#fff8c5;--muted:#6b7280;
  --r:10px;--sh:0 2px 8px rgba(0,0,0,.07)
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0d0d1a;--fg:#d8d8e8;--card:#161625;--border:#28283e;
  --accent:#7b8cff;--accent-h:#6a7bee;--accent-l:#1e1e38;
  --ok:#52b788;--ok-bg:#152e22;--err:#ff6b6b;--err-bg:#2e1515;
  --warn:#e3b341;--warn-bg:#2a2410;--muted:#9aa0b4;
  --sh:0 2px 8px rgba(0,0,0,.3)
}}
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  background:var(--bg);color:var(--fg);min-height:100vh;
  display:flex;justify-content:center;padding:32px 16px}
.wrap{max-width:560px;width:100%}
h1{font-size:1.4rem;font-weight:600;margin-bottom:8px;text-align:center;letter-spacing:-.02em}
.meta{text-align:center;font-size:.78rem;color:var(--muted);margin-bottom:18px;line-height:1.45}
.card{background:var(--card);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 20px;margin-bottom:14px;box-shadow:var(--sh)}
.lbl{font-size:.82rem;font-weight:500;opacity:.65;margin-bottom:6px}
.row{display:flex;gap:8px;flex-wrap:wrap}
.row>*{min-width:0}
input[type=text]{flex:1;padding:9px 12px;border:1px solid var(--border);
  border-radius:6px;font-size:.92rem;background:var(--bg);color:var(--fg);outline:none;
  transition:border-color .2s}
input[type=text]:focus{border-color:var(--accent)}
.btn{padding:10px 18px;border:none;border-radius:8px;font-size:.92rem;
  font-weight:500;cursor:pointer;background:var(--accent);color:#fff;
  transition:background .15s;white-space:nowrap}
.btn:hover{background:var(--accent-h)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-sm{padding:9px 14px;font-size:.82rem}
.btn-ghost{background:transparent;color:var(--fg);border:1px solid var(--border)}
.btn-ghost:hover{background:var(--accent-l);border-color:var(--accent)}
.btn-block{display:block;width:100%;padding:12px;font-size:1rem}
.actions{display:flex;gap:8px;margin-bottom:14px}
.actions .btn{flex:1}
ul.profiles{list-style:none}
.p-item{display:flex;align-items:center;gap:10px;
  padding:9px 12px;border:1px solid var(--border);border-radius:8px;
  margin-bottom:5px;cursor:pointer;transition:all .12s;user-select:none}
.p-item:hover{border-color:var(--accent);background:var(--accent-l)}
.p-item.sel{border-color:var(--accent);background:var(--accent-l);font-weight:500}
.p-item input[type=radio]{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
.p-idx{font-size:.78rem;opacity:.45;min-width:22px;text-align:right}
.p-host{flex:1;font-size:.92rem;overflow:hidden;text-overflow:ellipsis}
.p-lat{font-size:.75rem;font-variant-numeric:tabular-nums;min-width:72px;text-align:right}
.p-lat.ok{color:var(--ok)}
.p-lat.warn{color:var(--warn)}
.p-lat.err{color:var(--err)}
.p-lat.muted{color:var(--muted)}
.health{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  font-size:.78rem;font-weight:500}
.badge.ok{background:var(--ok-bg);color:var(--ok)}
.badge.err{background:var(--err-bg);color:var(--err)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}
.badge.muted{background:var(--border);color:var(--muted)}
.hint{text-align:center;padding:18px;opacity:.5;font-size:.88rem}
.msg{padding:12px 14px;border-radius:8px;font-size:.88rem;margin-top:12px;
  white-space:pre-wrap;word-break:break-word;display:none}
.msg.ok{display:block;background:var(--ok-bg);color:var(--ok)}
.msg.err{display:block;background:var(--err-bg);color:var(--err)}
.spin{display:inline-block;width:15px;height:15px;
  border:2px solid rgba(255,255,255,.3);border-top-color:#fff;
  border-radius:50%;animation:sp .55s linear infinite;vertical-align:middle;margin-right:8px}
.spin.dark{border-color:rgba(0,0,0,.15);border-top-color:var(--accent)}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Xray Gateway</h1>
  <div class="meta" id="meta">загрузка версий…</div>

  <div class="card">
    <div class="health">
      <div>
        <div class="lbl">Путь через VPN</div>
        <div id="pathHealth"><span class="badge muted">нет данных</span></div>
      </div>
      <button class="btn btn-sm btn-ghost" id="probePathBtn" onclick="runProbe('path')">Проверить путь</button>
    </div>
  </div>

  <div class="card">
    <div class="lbl">URL подписки</div>
    <div class="row">
      <input type="text" id="url" placeholder="https://...">
      <button class="btn btn-sm" id="loadBtn" onclick="loadProfiles()">Загрузить</button>
    </div>
  </div>

  <div class="card">
    <div class="health" style="margin-bottom:10px">
      <div class="lbl" style="margin:0">Профили <span id="probeHint" style="opacity:.7;font-weight:400"></span></div>
      <button class="btn btn-sm btn-ghost" id="probeProfilesBtn" onclick="runProbe('profiles')">Пинг серверов</button>
    </div>
    <div id="profiles"><div class="hint">Загрузка&#8230;</div></div>
  </div>

  <div class="actions">
    <button class="btn btn-block" id="applyBtn" onclick="apply()" disabled>Применить</button>
  </div>
  <div class="msg" id="msg"></div>
</div>

<script>
const S={url:'',idx:1,profiles:[],pathHealth:null,versions:null,probing:false};

function fmtMs(v){
  if(v==null||v===undefined)return '—';
  return Math.round(v)+'ms';
}
function latClass(ms, ok){
  if(!ok)return 'err';
  if(ms==null)return 'muted';
  if(ms<200)return 'ok';
  if(ms<500)return 'warn';
  return 'err';
}
function pathBadge(h){
  if(!h)return '<span class="badge muted">нет данных</span>';
  if(h.ok)return `<span class="badge ok">OK · ${fmtMs(h.ms)}</span>`;
  const err=esc(h.error||('HTTP '+h.httpCode)||'ошибка');
  return `<span class="badge err" title="${err}">FAIL · ${fmtMs(h.ms)}</span>`;
}
function shortVer(s){
  if(!s)return '—';
  const m=String(s).match(/Xray\s+([^\s]+)/i);
  if(m)return 'Xray '+m[1];
  return String(s).split(/\s+/)[0];
}

function renderMeta(){
  const v=S.versions||{};
  const x=shortVer(v.xray);
  const t=v.tun2socks||'—';
  document.getElementById('meta').textContent=`${x} · ${t}`;
}
function renderPath(){
  document.getElementById('pathHealth').innerHTML=pathBadge(S.pathHealth);
}
function render(){
  renderMeta();
  renderPath();
  const el=document.getElementById('profiles');
  const hint=document.getElementById('probeHint');
  if(S.probing)hint.textContent='· проверка…';
  else hint.textContent='';
  if(!S.profiles.length){
    el.innerHTML='<div class="hint">Нет профилей</div>';
    document.getElementById('applyBtn').disabled=true;
    return;
  }
  let h='<ul class="profiles">';
  for(const p of S.profiles){
    const s=p.index===S.idx?' sel':'';
    const c=p.index===S.idx?' checked':'';
    const pr=p.probe;
    let lat='—', cls='muted';
    if(pr){
      if(pr.ok){lat=fmtMs(pr.tcpMs);cls=latClass(pr.tcpMs,true);}
      else{lat='fail';cls='err';}
    }
    h+=`<li class="p-item${s}" onclick="sel(${p.index})">
      <input type="radio" name="p"${c}>
      <span class="p-idx">${p.index}</span>
      <span class="p-host">${esc(p.host)}</span>
      <span class="p-lat ${cls}" title="${pr&&pr.error?esc(pr.error):(pr?'TCP '+fmtMs(pr.tcpMs)+' / TLS '+fmtMs(pr.tlsMs):'')}"
      >${lat}</span></li>`;
  }
  h+='</ul>';
  el.innerHTML=h;
  document.getElementById('applyBtn').disabled=false;
}

function applyStatus(d){
  S.url=d.subscriptionUrl||S.url;
  S.idx=d.subscriptionIndex||S.idx;
  S.profiles=d.profiles||S.profiles;
  S.pathHealth=d.pathHealth||null;
  S.versions=d.versions||null;
  S.probing=!!(d.probingPath||d.probingProfiles);
  if(d.subscriptionUrl!==undefined)document.getElementById('url').value=S.url;
  render();
}

async function init(){
  try{
    const r=await fetch('/api/status');
    applyStatus(await r.json());
  }catch(e){
    document.getElementById('profiles').innerHTML='<div class="hint">Ошибка загрузки</div>';
  }
  setInterval(refreshQuiet, 15000);
}

async function refreshQuiet(){
  try{
    const r=await fetch('/api/status');
    applyStatus(await r.json());
  }catch(e){}
}

function sel(i){S.idx=i;render()}

async function loadProfiles(){
  const url=document.getElementById('url').value.trim();
  if(!url)return;
  S.url=url;
  const btn=document.getElementById('loadBtn');
  btn.disabled=true;btn.textContent='...';
  document.getElementById('profiles').innerHTML='<div class="hint">Загрузка профилей&#8230;</div>';
  try{
    const r=await fetch('/api/profiles',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
    const d=await r.json();
    if(d.ok){
      S.profiles=d.profiles;
      if(!S.profiles.find(p=>p.index===S.idx))S.idx=1;
      render();
    }else{
      document.getElementById('profiles').innerHTML=`<div class="hint">${esc(d.error||'Ошибка')}</div>`;
    }
  }catch(e){
    document.getElementById('profiles').innerHTML='<div class="hint">Ошибка сети</div>';
  }
  btn.disabled=false;btn.textContent='Загрузить';
}

async function runProbe(targets){
  const mode=targets||'all';
  const pathBtn=document.getElementById('probePathBtn');
  const profBtn=document.getElementById('probeProfilesBtn');
  const activeBtn=mode==='path'?pathBtn:mode==='profiles'?profBtn:null;
  const labelPath='Проверить путь';
  const labelProf='Пинг серверов';
  pathBtn.disabled=true;profBtn.disabled=true;
  if(activeBtn)activeBtn.innerHTML='<span class="spin dark"></span>';
  else{pathBtn.innerHTML='<span class="spin dark"></span>';profBtn.innerHTML='<span class="spin dark"></span>';}
  S.probing=true;render();
  try{
    const r=await fetch('/api/probe',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({targets:mode})});
    const d=await r.json();
    if(d.ok){
      if(d.pathHealth)S.pathHealth=d.pathHealth;
      if(d.versions)S.versions=d.versions;
      if(d.profiles)S.profiles=d.profiles;
    }
  }catch(e){}
  S.probing=false;
  pathBtn.disabled=false;profBtn.disabled=false;
  pathBtn.textContent=labelPath;profBtn.textContent=labelProf;
  render();
}

async function apply(){
  const btn=document.getElementById('applyBtn');
  const msg=document.getElementById('msg');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>Применяется\u2026';
  msg.className='msg';msg.style.display='none';
  try{
    const r=await fetch('/api/apply',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:S.url,index:S.idx})});
    const d=await r.json();
    if(d.ok){
      msg.className='msg ok';msg.style.display='block';
      let t='Конфиг обновлён';
      if(d.restarted)t+=', gateway перезапущен';
      if(d.output)t+='\n'+d.output;
      msg.textContent=t;
      setTimeout(()=>runProbe('path'), 5000);
    }else{
      msg.className='msg err';msg.style.display='block';
      msg.textContent=(d.error||'Ошибка')+(d.detail?'\n'+d.detail:'');
    }
  }catch(e){
    msg.className='msg err';msg.style.display='block';
    msg.textContent='Ошибка сети: '+e.message;
  }
  btn.disabled=false;btn.textContent='Применить';
}

function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "XrayGatewayWebUI/1.1"

    def log_message(self, fmt, *args):  # noqa: ARG002
        pass

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _html(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._html(_HTML)
        elif self.path == "/api/status":
            self._handle_status()
        elif self.path == "/api/health":
            self._handle_health()
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/apply":
            self._handle_apply()
        elif self.path == "/api/profiles":
            self._handle_profiles()
        elif self.path == "/api/probe":
            self._handle_probe()
        else:
            self.send_error(404)

    def _status_payload(self) -> dict:
        url = _read_env_value("SUBSCRIPTION_URL")
        raw_idx = _read_env_value("SUBSCRIPTION_INDEX") or "1"
        try:
            idx = int(raw_idx)
        except ValueError:
            idx = 1
        profiles = _enrich_profiles(_get_profiles(url) if url else [])
        snap = health_probe.STATE.snapshot()
        versions = snap.get("versions") or health_probe.refresh_versions(force=False)
        return {
            "subscriptionUrl": url,
            "subscriptionIndex": idx,
            "profiles": profiles,
            "pathHealth": snap.get("pathHealth"),
            "versions": versions,
            "probingPath": snap.get("probingPath"),
            "probingProfiles": snap.get("probingProfiles"),
        }

    def _handle_status(self) -> None:
        self._json(self._status_payload())

    def _handle_health(self) -> None:
        self._json(health_probe.STATE.snapshot())

    def _handle_profiles(self) -> None:
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._json({"ok": False, "error": "Некорректный JSON"}, 400)
            return
        url = (body.get("url") or "").strip()
        if not url:
            self._json({"ok": False, "error": "URL не задан"}, 400)
            return
        profiles = _enrich_profiles(_get_profiles(url))
        self._json({"ok": True, "profiles": profiles})

    def _handle_probe(self) -> None:
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._json({"ok": False, "error": "Некорректный JSON"}, 400)
            return
        targets = (body.get("targets") or "all").strip().lower()
        url = _read_env_value("SUBSCRIPTION_URL")
        profiles = _get_profiles(url) if url else []
        pathHealth = None
        versions = None
        if targets in ("path", "all"):
            pathHealth = health_probe.refresh_path_health(force=True)
        if targets in ("profiles", "all"):
            health_probe.refresh_profile_probes(profiles, force=True)
        if targets in ("versions", "all"):
            versions = health_probe.refresh_versions(force=True)
        self._json(
            {
                "ok": True,
                "pathHealth": pathHealth or health_probe.STATE.snapshot().get("pathHealth"),
                "profiles": _enrich_profiles(profiles),
                "versions": versions or health_probe.STATE.snapshot().get("versions"),
            }
        )

    def _handle_apply(self) -> None:
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._json({"ok": False, "error": "Некорректный JSON"}, 400)
            return
        url = (body.get("url") or "").strip()
        if not url:
            self._json({"ok": False, "error": "URL не задан"}, 400)
            return
        try:
            idx = int(body.get("index", 1))
        except (TypeError, ValueError):
            idx = 1
        try:
            self._json(_apply(url, idx))
        except Exception:
            self._json({"ok": False, "error": traceback.format_exc()[-500:]}, 500)


class _Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    # подтянуть HEALTH_* из .env, если compose не прокинул
    for key in (
        "HEALTH_PATH_URL",
        "HEALTH_PATH_INTERVAL",
        "HEALTH_PROFILE_INTERVAL",
        "HEALTH_PATH_TIMEOUT",
        "GATEWAY_CONTAINER_NAME",
    ):
        if not (os.environ.get(key) or "").strip():
            val = _read_env_value(key)
            if val:
                os.environ[key] = val

    port = int(os.environ.get("WEBUI_PORT", "8080"))
    health_probe.start_background(_current_profiles)
    srv = _Server(("0.0.0.0", port), _Handler)
    print(f"WebUI: http://0.0.0.0:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
