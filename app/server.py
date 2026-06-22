# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""Web service (FastAPI).

Routes (by role):
  - GET  /                 first-run setup, or the role's home
  - POST /api/role         set or switch the device role
  - POST /api/name         rename the device and sync the OS hostname
  - GET  /screen           the full-screen page the kiosk browser loads
  - GET  /api/screen-data  the playlist (and any active pairing code) for /screen
  - GET  /asset/{ref}      a cached image asset
  - content:  POST /api/content/url | /api/content/upload | /api/content/remove
  - display pairing:  POST /api/pair/start | /api/pair/cancel | /api/pair/claim
  - controller pairing:  GET /api/discover, POST /api/displays/add | /api/displays/remove

This service listens on the LAN. Login still needs to gate it before release.
"""

import html as _htmllib
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from . import config, discovery, hostname, identity, library, pairing

_SETUP_HTML = (Path(__file__).parent / "pages" / "setup.html").read_text(encoding="utf-8")
_SCREEN_HTML = (Path(__file__).parent / "pages" / "screen.html").read_text(encoding="utf-8")


def create_app() -> FastAPI:
    app = FastAPI(title="signage")
    device_id = identity.get_or_create_device_id()

    def current() -> dict:
        cfg = config.load_config()
        if not cfg.get("name"):
            cfg["name"] = identity.default_name(device_id)
        return cfg

    @app.on_event("startup")
    def _advertise():
        # Best-effort LAN advertisement so controllers can discover this device.
        # Discovery must never block or crash the app, so failures are swallowed.
        try:
            cfg = current()
            app.state.zc = discovery.advertise(
                device_id, cfg["name"], cfg.get("role") or "unset", config.PORT
            )
        except Exception:
            app.state.zc = None

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "device_id": device_id}

    @app.get("/", response_class=HTMLResponse)
    def home():
        cfg = current()
        role = cfg.get("role")
        if role not in config.VALID_ROLES:
            return _splash(cfg)
        if role == "controller":
            return _control_home(cfg)
        return _display_home(cfg)

    @app.post("/api/role")
    def set_role(role: str = Form(...)):
        if role not in config.VALID_ROLES:
            return JSONResponse({"error": "invalid role"}, status_code=400)
        cfg = current()
        cfg["role"] = role
        config.save_config(cfg)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/name")
    def set_name(name: str = Form(...)):
        cfg = current()
        cfg["name"] = name.strip() or cfg["name"]
        config.save_config(cfg)
        if cfg.get("sync_hostname", True):
            hostname.apply_hostname(cfg["name"])
        return RedirectResponse("/", status_code=303)

    # --- the screen the kiosk shows -----------------------------------------

    @app.get("/screen", response_class=HTMLResponse)
    def screen():
        return HTMLResponse(_SCREEN_HTML)

    @app.get("/api/screen-data")
    def screen_data():
        out = []
        for item in library.list_items():
            src = item["ref"] if item["type"] == "url" else f"/asset/{item['ref']}"
            out.append({"type": item["type"], "src": src, "seconds": item["seconds"]})
        return {"items": out, "pairing_code": pairing.current_code()}

    @app.get("/asset/{ref}")
    def asset(ref: str):
        path = library.asset_path(ref)
        if "/" in ref or "\\" in ref or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    # --- content ------------------------------------------------------------

    @app.post("/api/content/url")
    def add_url(url: str = Form(...), seconds: int = Form(library.DEFAULT_URL_SECONDS)):
        library.add_url(url, seconds)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/upload")
    async def add_upload(file: UploadFile, seconds: int = Form(library.DEFAULT_IMAGE_SECONDS)):
        data = await file.read()
        name = file.filename or "upload"
        try:
            if name.lower().endswith((".pptx", ".ppt")):
                library.add_pptx(name, data, seconds)
            else:
                library.add_image(name, data, seconds)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/remove")
    def remove_content(item_id: str = Form(...)):
        library.remove(item_id)
        return RedirectResponse("/", status_code=303)

    # --- pairing: display side ----------------------------------------------

    @app.post("/api/pair/start")
    def pair_start():
        pairing.start_pairing()
        return RedirectResponse("/", status_code=303)

    @app.post("/api/pair/cancel")
    def pair_cancel():
        pairing.cancel_pairing()
        return RedirectResponse("/", status_code=303)

    @app.post("/api/pair/claim")
    async def pair_claim(request: Request):
        body = await request.json()
        try:
            result = pairing.claim(
                body["code"], body["controller"], body["sealed_site_key"]
            )
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"error": "claim failed"}, status_code=400)
        result["name"] = current()["name"]
        return result

    # --- pairing: controller side -------------------------------------------

    @app.get("/api/discover")
    def discover():
        try:
            devices = discovery.browse(timeout=3.0)
        except Exception:
            devices = []
        paired = {d["device_id"] for d in pairing.list_displays()}
        out = [d for d in devices if d.get("device_id") and d["device_id"] != device_id]
        for d in out:
            d["paired"] = d["device_id"] in paired
        return {"devices": out}

    @app.post("/api/displays/add")
    def displays_add(
        address: str = Form(...),
        code: str = Form(...),
        port: int = Form(8080),
    ):
        cfg = current()
        controller_meta = {
            "device_id": device_id,
            "name": cfg["name"],
            "address": discovery.primary_ip(),
        }
        try:
            pairing.claim_display(address, port, code, controller_meta)
        except Exception as exc:
            return JSONResponse({"error": f"Could not pair: {exc}"}, status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/displays/remove")
    def displays_remove(device: str = Form(..., alias="device_id")):
        pairing.remove_display(device)
        return RedirectResponse("/", status_code=303)

    return app


# --- built-in HTML pages (basic styling; the main control panel is separate) ---

def _esc(text) -> str:
    return _htmllib.escape(str(text))


def _page(title: str, body: str, *, dark: bool = False) -> HTMLResponse:
    bg, fg = ("#0b0b0c", "#f4f4f5") if dark else ("#f7f7f8", "#18181b")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:{bg};color:{fg};
       min-height:100vh;display:flex;justify-content:center}}
  .wrap{{width:100%;max-width:640px;padding:32px}}
  h1{{font-size:1.6rem;margin:0 0 .25rem}}
  a{{color:#2563eb}}
  .muted{{opacity:.6;font-size:.9rem}}
  .card{{background:#fff;border:1px solid #e4e4e7;border-radius:14px;padding:18px;margin:14px 0}}
  .card b{{display:block;margin-bottom:10px}}
  .row{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}}
  .row input[name=url]{{flex:1;min-width:200px}}
  input{{padding:10px;border:1px solid #d4d4d8;border-radius:9px;font:inherit}}
  input[type=number]{{width:90px}}
  button{{border:0;border-radius:9px;padding:10px 16px;background:#2563eb;color:#fff;font:inherit;cursor:pointer}}
  .item{{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #eee}}
  .item span:first-child{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .item .x{{background:#ef4444;padding:4px 10px}}
  .code{{font:700 2rem/1.2 ui-monospace,monospace;letter-spacing:.18em;text-align:center;
         background:#f1f5f9;border:1px solid #e4e4e7;border-radius:12px;padding:16px;margin:10px 0}}
</style></head>
<body><div class="wrap">{body}</div></body></html>"""
    return HTMLResponse(html)


def _splash(cfg: dict) -> HTMLResponse:
    return HTMLResponse(_SETUP_HTML.replace("__DEVICE_NAME__", cfg["name"]))


def _content_body(cfg: dict, role_label: str) -> str:
    items = library.list_items()
    if items:
        rows = ""
        for it in items:
            rows += f"""
              <div class="item">
                <span>{_esc(it['name'])}</span>
                <span class="muted">{it['seconds']}s</span>
                <form method="post" action="/api/content/remove" style="margin:0">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <button class="x" title="Remove">&times;</button>
                </form>
              </div>"""
        playlist = f'<div class="card"><b>Playlist</b>{rows}</div>'
    else:
        playlist = ('<div class="card"><b>Playlist</b><br>'
                    '<span class="muted">Nothing yet — add a link or upload below.</span></div>')

    return f"""
      <h1>{role_label}</h1>
      <p class="muted">{_esc(cfg['name'])} · <a href="/screen">open full-screen view</a></p>

      <div class="card">
        <b>Add a web page or Google Slides</b>
        <form method="post" action="/api/content/url" class="row">
          <input name="url" placeholder="https://… or a Google Slides 'Publish to web' link" required>
          <input name="seconds" type="number" min="3" value="15" title="seconds">
          <button>Add</button>
        </form>
        <span class="muted">For Google Slides: File → Share → Publish to web, then paste the link.</span>
      </div>

      <div class="card">
        <b>Upload an image or PowerPoint</b>
        <form method="post" action="/api/content/upload" enctype="multipart/form-data" class="row">
          <input name="file" type="file" accept="image/*,.pptx,.ppt" required>
          <input name="seconds" type="number" min="3" value="10" title="seconds per image">
          <button>Upload</button>
        </form>
        <span class="muted">PowerPoint is converted to slides automatically.</span>
      </div>

      {playlist}
    """


def _display_home(cfg: dict) -> HTMLResponse:
    code = pairing.current_code()
    if code:
        section = f"""
          <div class="card"><b>Pairing</b>
            <p>On your controller, pick this display and enter this code:</p>
            <div class="code">{code}</div>
            <p class="muted">Valid for 3 minutes. It also shows on the screen itself.</p>
            <form method="post" action="/api/pair/cancel"><button class="x">Cancel</button></form>
          </div>"""
    elif pairing.is_claimed():
        controller = pairing.get_controller() or {}
        section = f"""
          <div class="card"><b>Paired</b><br>
            <span class="muted">Controlled by {_esc(controller.get('name') or 'a controller')}.</span>
            <form method="post" action="/api/pair/start" style="margin-top:10px"><button>Re-pair</button></form>
          </div>"""
    else:
        section = """
          <div class="card"><b>Pair to a controller</b>
            <p class="muted">Start pairing, then enter the code on your controller.</p>
            <form method="post" action="/api/pair/start"><button>Start pairing</button></form>
          </div>"""
    return _page("Display", section + _content_body(cfg, "Display"))


def _control_home(cfg: dict) -> HTMLResponse:
    displays = pairing.list_displays()
    if displays:
        rows = ""
        for d in displays:
            rows += f"""
              <div class="item">
                <span>{_esc(d['name'] or d['device_id'][:8])}</span>
                <span class="muted">{_esc(d['address'])}</span>
                <form method="post" action="/api/displays/remove" style="margin:0">
                  <input type="hidden" name="device_id" value="{d['device_id']}">
                  <button class="x" title="Unpair">&times;</button>
                </form>
              </div>"""
        screens = f'<div class="card"><b>Your displays</b>{rows}</div>'
    else:
        screens = ('<div class="card"><b>Your displays</b><br>'
                   '<span class="muted">None paired yet — find one below.</span></div>')

    find = """
      <div class="card"><b>Find displays</b>
        <button onclick="findDisplays()">Refresh</button>
        <div id="found" class="muted" style="margin-top:12px">Tap Refresh to scan the network.</div>
        <details style="margin-top:12px">
          <summary class="muted">Add by address (fallback)</summary>
          <form method="post" action="/api/displays/add" class="row" style="margin-top:8px">
            <input name="address" placeholder="192.168.1.50" required>
            <input name="port" type="number" value="8080">
            <input name="code" placeholder="CODE" required style="width:120px">
            <button>Pair</button>
          </form>
        </details>
      </div>
      <script>
      async function findDisplays(){
        const box=document.getElementById('found');
        box.textContent='Scanning…';
        try{
          const r=await fetch('/api/discover');
          const d=await r.json();
          const list=(d.devices||[]).filter(x=>x.role!=='controller');
          if(!list.length){box.textContent='No displays found. Start pairing on the display, or use Add by address.';return;}
          box.innerHTML=list.map(x=>`<div class="item">
            <span>${x.name||x.address}</span><span class="muted">${x.address}</span>
            ${x.paired?'<span class="muted">paired</span>':
              `<form method="post" action="/api/displays/add" style="margin:0">
                 <input type="hidden" name="address" value="${x.address}">
                 <input type="hidden" name="port" value="${x.port}">
                 <input name="code" placeholder="CODE" required style="width:110px">
                 <button>Pair</button>
               </form>`}
          </div>`).join('');
        }catch(e){box.textContent='Scan failed.';}
      }
      </script>"""

    return _page("Controller", screens + find + _content_body(cfg, "Controller"))
