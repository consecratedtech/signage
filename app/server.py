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

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, hostname, identity

_SETUP_HTML = (Path(__file__).parent / "pages" / "setup.html").read_text(encoding="utf-8")


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
        return _screen_page(current())

    return app


# --- built-in HTML pages (basic styling; the main control panel is separate) ---

def _page(title: str, body: str, *, dark: bool = False) -> HTMLResponse:
    bg, fg = ("#0b0b0c", "#f4f4f5") if dark else ("#f7f7f8", "#18181b")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:{bg};color:{fg};
       min-height:100vh;display:flex;align-items:center;justify-content:center}}
  .wrap{{width:100%;max-width:640px;padding:32px;text-align:center}}
  h1{{font-size:1.6rem;margin:0 0 .25rem}}
  .muted{{opacity:.6}}
  .btn{{display:inline-block;border:0;border-radius:12px;padding:18px 28px;
        margin:10px;font-size:1.1rem;cursor:pointer;background:#2563eb;color:#fff}}
  .card{{background:#fff;border:1px solid #e4e4e7;border-radius:14px;padding:20px;
         margin:14px 0;text-align:left}}
</style></head>
<body><div class="wrap">{body}</div></body></html>"""
    return HTMLResponse(html)


def _splash(cfg: dict) -> HTMLResponse:
    html = _SETUP_HTML.replace("__DEVICE_NAME__", cfg["name"])
    return HTMLResponse(html)


def _control_home(cfg: dict) -> HTMLResponse:
    body = f"""
      <h1>Controller</h1>
      <p class="muted">{cfg['name']}</p>
      <div class="card"><b>Your screens</b><br>
        <span class="muted">No displays paired yet.</span>
      </div>
      <div class="card"><b>+ Add content</b><br>
        <span class="muted">Web page, images, Google Slides, or PowerPoint.</span>
      </div>
    """
    return _page("Controller", body)


def _display_home(cfg: dict) -> HTMLResponse:
    body = f"""
      <h1>Display</h1>
      <p class="muted">{cfg['name']}</p>
      <div class="card">This device shows content on its screen.
        The full-screen view is at <a href="/screen">/screen</a>.<br>
        <span class="muted">Not paired to a controller yet.</span>
      </div>
    """
    return _page("Display", body)


def _screen_page(cfg: dict) -> HTMLResponse:
    body = f"""
      <h1>{cfg['name']}</h1>
      <p class="muted">Unclaimed — open the controller on this network to pair.</p>
    """
    return _page("Screen", body, dark=True)
