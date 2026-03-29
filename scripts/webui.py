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

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_config  # noqa: E402


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
    return [
        {"index": i, "host": generate_config.vless_uri_host(line) or "?"}
        for i, line in enumerate(lines, 1)
    ]


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
  --r:10px;--sh:0 2px 8px rgba(0,0,0,.07)
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0d0d1a;--fg:#d8d8e8;--card:#161625;--border:#28283e;
  --accent:#7b8cff;--accent-h:#6a7bee;--accent-l:#1e1e38;
  --ok:#52b788;--ok-bg:#152e22;--err:#ff6b6b;--err-bg:#2e1515;
  --sh:0 2px 8px rgba(0,0,0,.3)
}}
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  background:var(--bg);color:var(--fg);min-height:100vh;
  display:flex;justify-content:center;padding:32px 16px}
.wrap{max-width:520px;width:100%}
h1{font-size:1.4rem;font-weight:600;margin-bottom:20px;text-align:center;letter-spacing:-.02em}
.card{background:var(--card);border:1px solid var(--border);
  border-radius:var(--r);padding:18px 20px;margin-bottom:14px;box-shadow:var(--sh)}
.lbl{font-size:.82rem;font-weight:500;opacity:.65;margin-bottom:6px}
.row{display:flex;gap:8px}
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
.btn-block{display:block;width:100%;padding:12px;font-size:1rem}
ul.profiles{list-style:none}
.p-item{display:flex;align-items:center;gap:10px;
  padding:9px 12px;border:1px solid var(--border);border-radius:8px;
  margin-bottom:5px;cursor:pointer;transition:all .12s;user-select:none}
.p-item:hover{border-color:var(--accent);background:var(--accent-l)}
.p-item.sel{border-color:var(--accent);background:var(--accent-l);font-weight:500}
.p-item input[type=radio]{accent-color:var(--accent);width:15px;height:15px;cursor:pointer}
.p-idx{font-size:.78rem;opacity:.45;min-width:22px;text-align:right}
.p-host{flex:1;font-size:.92rem}
.hint{text-align:center;padding:18px;opacity:.5;font-size:.88rem}
.msg{padding:12px 14px;border-radius:8px;font-size:.88rem;margin-top:12px;
  white-space:pre-wrap;word-break:break-word;display:none}
.msg.ok{display:block;background:var(--ok-bg);color:var(--ok)}
.msg.err{display:block;background:var(--err-bg);color:var(--err)}
.spin{display:inline-block;width:15px;height:15px;
  border:2px solid rgba(255,255,255,.3);border-top-color:#fff;
  border-radius:50%;animation:sp .55s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Xray Gateway</h1>

  <div class="card">
    <div class="lbl">URL подписки</div>
    <div class="row">
      <input type="text" id="url" placeholder="https://...">
      <button class="btn btn-sm" id="loadBtn" onclick="loadProfiles()">Загрузить</button>
    </div>
  </div>

  <div class="card">
    <div class="lbl">Профили</div>
    <div id="profiles"><div class="hint">Загрузка&#8230;</div></div>
  </div>

  <button class="btn btn-block" id="applyBtn" onclick="apply()" disabled>Применить</button>
  <div class="msg" id="msg"></div>
</div>

<script>
const S={url:'',idx:1,profiles:[]};

async function init(){
  try{
    const r=await fetch('/api/status');
    const d=await r.json();
    S.url=d.subscriptionUrl||'';
    S.idx=d.subscriptionIndex||1;
    S.profiles=d.profiles||[];
    document.getElementById('url').value=S.url;
    render();
  }catch(e){
    document.getElementById('profiles').innerHTML='<div class="hint">Ошибка загрузки</div>';
  }
}

function render(){
  const el=document.getElementById('profiles');
  if(!S.profiles.length){
    el.innerHTML='<div class="hint">Нет профилей</div>';
    document.getElementById('applyBtn').disabled=true;
    return;
  }
  let h='<ul class="profiles">';
  for(const p of S.profiles){
    const s=p.index===S.idx?' sel':'';
    const c=p.index===S.idx?' checked':'';
    h+=`<li class="p-item${s}" onclick="sel(${p.index})">
      <input type="radio" name="p"${c}>
      <span class="p-idx">${p.index}</span>
      <span class="p-host">${esc(p.host)}</span></li>`;
  }
  h+='</ul>';
  el.innerHTML=h;
  document.getElementById('applyBtn').disabled=false;
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

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "XrayGatewayWebUI/1.0"

    def log_message(self, fmt, *args):  # noqa: ARG002
        pass

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    # --- routes ---

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._html(_HTML)
        elif self.path == "/api/status":
            self._handle_status()
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/apply":
            self._handle_apply()
        elif self.path == "/api/profiles":
            self._handle_profiles()
        else:
            self.send_error(404)

    def _handle_status(self) -> None:
        url = _read_env_value("SUBSCRIPTION_URL")
        raw_idx = _read_env_value("SUBSCRIPTION_INDEX") or "1"
        try:
            idx = int(raw_idx)
        except ValueError:
            idx = 1
        profiles = _get_profiles(url) if url else []
        self._json({"subscriptionUrl": url, "subscriptionIndex": idx, "profiles": profiles})

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
        self._json({"ok": True, "profiles": _get_profiles(url)})

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
    port = int(os.environ.get("WEBUI_PORT", "8080"))
    srv = _Server(("0.0.0.0", port), _Handler)
    print(f"WebUI: http://0.0.0.0:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
