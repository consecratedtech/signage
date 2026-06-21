# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""Web service (FastAPI).

Routes:
  - GET /          first-run setup (Display or Controller) or the role's home
  - POST /api/role set or switch the device role, persisted to config
  - POST /api/name rename the device and sync the OS hostname
  - GET /screen    the full-screen page the kiosk browser loads on a display

This service listens on the LAN without authentication. Pairing and login
gate it before any public release.
"""

from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from . import config, hostname, identity, library

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
            # network sees the friendly name; pairing rides on the Device ID,
            # so this rename can't break a paired link.
            hostname.apply_hostname(cfg["name"])
        return RedirectResponse("/", status_code=303)

    @app.get("/screen", response_class=HTMLResponse)
    def screen():
        return HTMLResponse(_SCREEN_HTML)

    # --- content ------------------------------------------------------------

    @app.get("/api/screen-data")
    def screen_data():
        """The playlist the rotator plays, as simple {type, src, seconds}."""
        out = []
        for item in library.list_items():
            src = item["ref"] if item["type"] == "url" else f"/asset/{item['ref']}"
            out.append({"type": item["type"], "src": src, "seconds": item["seconds"]})
        return {"items": out}

    @app.get("/asset/{ref}")
    def asset(ref: str):
        path = library.asset_path(ref)
        if "/" in ref or "\\" in ref or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

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

    return app


# --- built-in HTML pages (basic styling; the main control panel is separate) ---

import html as _htmllib


def _esc(text: str) -> str:
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
  .card{{background:#fff;border:1px solid #e4e4e7;border-radius:14px;padding:18px;
         margin:14px 0}}
  .card b{{display:block;margin-bottom:10px}}
  .row{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}}
  .row input[type=text],.row input:not([type]),.row input[name=url]{{flex:1;min-width:200px}}
  input{{padding:10px;border:1px solid #d4d4d8;border-radius:9px;font:inherit}}
  input[type=number]{{width:90px}}
  button{{border:0;border-radius:9px;padding:10px 16px;background:#2563eb;color:#fff;
          font:inherit;cursor:pointer}}
  .item{{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #eee}}
  .item span:first-child{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .item .x{{background:#ef4444;padding:4px 10px}}
</style></head>
<body><div class="wrap">{body}</div></body></html>"""
    return HTMLResponse(html)


def _splash(cfg: dict) -> HTMLResponse:
    html = _SETUP_HTML.replace("__DEVICE_NAME__", cfg["name"])
    return HTMLResponse(html)


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
        playlist = '<div class="card"><b>Playlist</b><br><span class="muted">Nothing yet — add a link or upload below.</span></div>'

    return f"""
      <h1>{role_label}</h1>
      <p class="muted">{cfg['name']} · <a href="/screen">open full-screen view</a></p>

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


def _control_home(cfg: dict) -> HTMLResponse:
    return _page("Controller", _content_body(cfg, "Controller"))


def _display_home(cfg: dict) -> HTMLResponse:
    return _page("Display", _content_body(cfg, "Display"))
