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
import io
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from . import (
    activity,
    auth,
    commands,
    config,
    discovery,
    hostname,
    identity,
    library,
    pairing,
    sync,
)

_SETUP_HTML = (Path(__file__).parent / "pages" / "setup.html").read_text(encoding="utf-8")
_SCREEN_HTML = (Path(__file__).parent / "pages" / "screen.html").read_text(encoding="utf-8")


def create_app() -> FastAPI:
    device_id = identity.get_or_create_device_id()

    def current() -> dict:
        cfg = config.load_config()
        if not cfg.get("name"):
            cfg["name"] = identity.default_name(device_id)
        return cfg

    def _advertise_now(app: FastAPI) -> None:
        # Best-effort LAN advertisement so controllers can discover this device.
        # Discovery must never block or crash the app, so failures are swallowed.
        try:
            cfg = current()
            app.state.zc = discovery.advertise(
                device_id, cfg["name"], cfg.get("role") or "unset", config.PORT
            )
        except Exception:
            app.state.zc = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Replaces the deprecated @app.on_event("startup") and also tears the
        # mDNS advertisement down cleanly on shutdown.
        _advertise_now(app)
        try:
            yield
        finally:
            zc = getattr(app.state, "zc", None)
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass

    app = FastAPI(title="signage", lifespan=lifespan)

    def _readvertise() -> None:
        # Role or name changed — re-publish so discovery reflects the current
        # state instead of the value captured at boot.
        zc = getattr(app.state, "zc", None)
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass
        _advertise_now(app)

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
        _readvertise()
        activity.log(f"Switched this device's role to {role}")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/name")
    def set_name(name: str = Form(...)):
        cfg = current()
        cfg["name"] = name.strip() or cfg["name"]
        config.save_config(cfg)
        if cfg.get("sync_hostname", True):
            hostname.apply_hostname(cfg["name"])
        _readvertise()
        activity.log(f"Renamed this device to {cfg['name']}")
        return RedirectResponse("/", status_code=303)

    # --- the screen the kiosk shows -----------------------------------------

    @app.get("/screen", response_class=HTMLResponse)
    def screen():
        return HTMLResponse(_SCREEN_HTML)

    @app.get("/api/screen-data")
    def screen_data():
        pushed = sync.screen_items()  # None until a controller pushes content
        if pushed is not None:
            items = pushed
        else:
            items = []
            for item in library.list_items():
                src = item["ref"] if item["type"] == "url" else f"/asset/{item['ref']}"
                items.append({"type": item["type"], "src": src, "seconds": item["seconds"]})
        return {
            "items": items,
            "pairing_code": pairing.current_code(),
            "shuffle": bool(current().get("shuffle")),
            # so a display with nothing on it can show where to connect
            "connect_url": f"http://{discovery.primary_ip()}:{config.PORT}",
        }

    @app.get("/recv-asset/{name}")
    def recv_asset(name: str):
        path = sync.recv_asset_path(name)
        if "/" in name or "\\" in name or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/asset/{ref}")
    def asset(ref: str):
        path = library.asset_path(ref)
        if "/" in ref or "\\" in ref or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/qr.png")
    def qr_png():
        """A QR code for this device's address, shown on the idle/setup screen so
        someone can open the control panel by scanning instead of typing an IP.
        The encoded address is always this device's own — never client input."""
        try:
            import qrcode  # local import: a missing optional dep must not block boot
        except Exception:
            return Response(status_code=404)
        url = f"http://{discovery.primary_ip()}:{config.PORT}"
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    # --- content ------------------------------------------------------------

    @app.post("/api/content/url")
    def add_url(url: str = Form(...), seconds: int = Form(library.DEFAULT_URL_SECONDS)):
        library.add_url(url, seconds)
        activity.log("Added a link to the playlist", url)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/upload")
    async def add_upload(file: UploadFile, seconds: int = Form(library.DEFAULT_IMAGE_SECONDS)):
        data = await file.read()
        name = file.filename or "upload"
        try:
            if name.lower().endswith((".pptx", ".ppt")):
                added = library.add_pptx(name, data, seconds)
                activity.log(f"Added a PowerPoint ({len(added)} slide(s))", name)
            else:
                library.add_image(name, data, seconds)
                activity.log("Added an image to the playlist", name)
        except RuntimeError as exc:
            activity.log("An upload could not be processed", str(exc))
            return JSONResponse({"error": str(exc)}, status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/remove")
    def remove_content(item_id: str = Form(...)):
        library.remove(item_id)
        activity.log("Removed an item from the playlist")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/seconds")
    def content_seconds(item_id: str = Form(...), seconds: int = Form(...)):
        library.set_seconds(item_id, seconds)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/move")
    def content_move(item_id: str = Form(...), direction: str = Form(...)):
        if direction in ("up", "down"):
            library.move(item_id, direction)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/playback")
    def set_playback(shuffle: str = Form(default="")):
        cfg = current()
        cfg["shuffle"] = bool(shuffle)
        config.save_config(cfg)
        activity.log("Shuffle turned " + ("on" if cfg["shuffle"] else "off"))
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
        activity.log("Paired with a controller", body.get("controller", {}).get("name", ""))
        return result

    @app.post("/api/playlist")
    async def receive_playlist(request: Request):
        controller = pairing.get_controller()
        if not controller:
            return JSONResponse({"error": "not paired"}, status_code=403)
        body = await request.json()
        ok = sync.receive(body.get("manifest"), body.get("signature"), controller["site_key"])
        if not ok:
            return JSONResponse({"error": "rejected"}, status_code=403)
        return {"ok": True}

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
            record = pairing.claim_display(address, port, code, controller_meta)
        except Exception as exc:
            activity.log("A pairing attempt failed", str(exc))
            return JSONResponse({"error": f"Could not pair: {exc}"}, status_code=400)
        activity.log("Paired a display", record.get("name") or address)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/displays/remove")
    def displays_remove(device: str = Form(..., alias="device_id")):
        pairing.remove_display(device)
        activity.log("Unpaired a display")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/push")
    def push():
        displays = pairing.list_displays()
        if not displays:
            return {"results": [], "message": "No displays paired yet."}
        base_url = f"http://{discovery.primary_ip()}:{config.PORT}"
        manifest = sync.build_manifest(library.list_items(), current()["name"], base_url)
        results = sync.push_all(displays, manifest, auth.get_or_create_site_key())
        ok = sum(1 for r in results if r.get("ok"))
        activity.log(f"Pushed content to {ok} of {len(results)} display(s)")
        return {"results": results}

    return app


# --- built-in HTML pages (styled to match pages/setup.html design language) ---

def _esc(text) -> str:
    return _htmllib.escape(str(text))


# Google Fonts used across the control panel (mirrors pages/setup.html).
_FONTS_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700'
    '&family=Hanken+Grotesk:wght@400;500;600&family=Space+Mono'
    '&display=swap" rel="stylesheet">'
)

# Big stylesheet kept as a plain (non-f) string so CSS braces need no escaping.
_CSS = """
  :root{
    --paper:#F4F6F3; --ink:#17211E; --pine:#0F5D54; --pine-deep:#0A4339;
    --glow:#F0A93B; --glow-soft:rgba(240,169,59,.18); --line:#DBE0DA;
    --muted:#5C6661; --card:#FFFFFF; --radius:18px;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:var(--paper); color:var(--ink);
    font:16px/1.55 "Hanken Grotesk",system-ui,sans-serif;
    min-height:100vh; padding:24px;
    -webkit-font-smoothing:antialiased;
  }
  .stage{width:100%;max-width:720px;margin:0 auto}
  a{color:var(--pine);text-decoration:none}
  a:hover{text-decoration:underline}

  /* header */
  .top{
    display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    margin-bottom:22px;
  }
  .brand{
    display:flex;align-items:center;gap:9px;
    font-weight:600;color:var(--muted);letter-spacing:.01em;
  }
  .brand .mark{
    width:18px;height:18px;border-radius:50%;flex:none;
    background:conic-gradient(from 220deg,var(--pine),var(--glow),var(--pine));
  }
  .logo-ph{
    display:inline-flex;align-items:center;justify-content:center;
    min-width:64px;height:34px;padding:0 12px;
    border:1.5px dashed var(--line);border-radius:10px;
    font-family:"Space Mono",monospace;font-size:.62rem;letter-spacing:.16em;
    color:var(--muted);text-transform:uppercase;
  }
  .name-chip{
    font-family:"Space Mono",monospace;font-size:.8rem;color:var(--muted);
    background:var(--card);border:1px solid var(--line);
    padding:6px 10px;border-radius:9px;white-space:nowrap;
  }
  .badge{
    font-family:"Space Mono",monospace;font-size:.66rem;letter-spacing:.14em;
    text-transform:uppercase;padding:6px 11px;border-radius:999px;
    border:1px solid var(--line);color:var(--pine);background:var(--glow-soft);
  }
  .badge.controller{color:var(--pine-deep)}
  .badge.display{color:var(--pine)}
  .top .spacer{flex:1}
  .gear{
    border:1px solid var(--line);background:var(--card);color:var(--ink);
    font:inherit;font-weight:600;font-size:.9rem;cursor:pointer;
    padding:9px 14px;border-radius:11px;
    transition:background .15s ease,border-color .15s ease;
  }
  .gear:hover{background:var(--paper);border-color:var(--muted)}

  /* page intro */
  .eyebrow{
    font-family:"Space Mono",monospace;font-size:.72rem;letter-spacing:.22em;
    text-transform:uppercase;color:var(--pine);margin:0 0 8px;
  }
  h1{
    font-family:"Bricolage Grotesque",sans-serif;font-weight:700;
    font-size:clamp(1.7rem,4vw,2.3rem);line-height:1.06;letter-spacing:-.02em;
    margin:0 0 6px;
  }
  .lead{color:var(--muted);margin:0 0 22px}

  /* cards */
  .card{
    background:var(--card);border:1px solid var(--line);
    border-radius:var(--radius);padding:20px 22px;margin:0 0 18px;
  }
  .card h2{
    font-family:"Bricolage Grotesque",sans-serif;font-weight:600;
    font-size:1.12rem;letter-spacing:-.01em;margin:0 0 4px;
    display:flex;align-items:center;gap:8px;
  }
  .card .hint{color:var(--muted);font-size:.9rem;margin:0 0 14px}
  .card .hint.tail{margin:12px 0 0}

  /* forms */
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  label.fld{display:flex;flex-direction:column;gap:5px;font-size:.85rem;color:var(--muted)}
  input,select{
    font:inherit;color:var(--ink);background:var(--card);
    padding:11px 12px;border:1px solid var(--line);border-radius:11px;
  }
  input:focus,select:focus{outline:none;border-color:var(--pine)}
  input[name=url]{flex:1;min-width:220px}
  input[type=number]{width:96px}
  input[type=file]{padding:9px 11px}

  /* buttons */
  button{font:inherit;font-weight:600;cursor:pointer;border:0;border-radius:11px;
    padding:11px 17px;transition:background .15s ease,transform .12s ease,border-color .15s ease}
  button:active{transform:translateY(1px)}
  .btn-primary{background:var(--pine);color:#fff}
  .btn-primary:hover{background:var(--pine-deep)}
  .btn-accent{background:var(--glow);color:var(--ink)}
  .btn-accent:hover{background:#e09a2c}
  .btn-ghost{background:var(--card);color:var(--ink);border:1px solid var(--line)}
  .btn-ghost:hover{background:var(--paper);border-color:var(--muted)}
  .btn-danger{background:#fff;color:#b42318;border:1px solid #f1c9c4}
  .btn-danger:hover{background:#fdecea;border-color:#e29a93}

  /* lists (playlist items, displays, discovered devices) */
  .item{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
  .item:first-of-type{border-top:0}
  .item .name{flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .item .meta{font-family:"Space Mono",monospace;font-size:.78rem;color:var(--muted)}
  .item form{margin:0}
  .item .secs{display:flex;align-items:center;gap:6px;font-size:.8rem;color:var(--muted)}
  .item .secs input{width:74px;padding:8px 9px}
  .x{background:#fff;color:#b42318;border:1px solid #f1c9c4;
     padding:7px 12px;border-radius:9px;line-height:1}
  .x:hover{background:#fdecea;border-color:#e29a93}
  .empty{color:var(--muted)}
  /* reorder arrows + per-item second editing + shuffle toggle */
  .item .ord{display:flex;flex-direction:column;gap:3px;flex:none}
  .mv{background:var(--card);border:1px solid var(--line);color:var(--muted);
      padding:1px 9px;border-radius:7px;line-height:1.15;font-size:.66rem}
  .mv:hover:not(:disabled){border-color:var(--pine);color:var(--pine)}
  .mv:disabled{opacity:.3;cursor:default}
  .item .secs .set{padding:8px 12px}
  .shuffle{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
           margin:0 0 12px;padding:0 0 14px;border-bottom:1px solid var(--line)}
  .shuffle label{display:flex;align-items:center;gap:8px;font-weight:500;cursor:pointer}
  .shuffle input[type=checkbox]{width:18px;height:18px;accent-color:var(--pine)}

  /* pairing code */
  .code{
    font-family:"Space Mono",monospace;font-weight:700;font-size:2.1rem;
    letter-spacing:.2em;text-align:center;color:var(--pine-deep);
    background:var(--paper);border:1px solid var(--line);
    border-radius:14px;padding:18px;margin:6px 0 14px;
  }
  .status{display:flex;align-items:center;gap:10px;color:var(--muted);margin:0 0 12px}
  .status .pulse{position:relative;flex:none;width:12px;height:12px}
  .status .pulse i{position:absolute;inset:0;border-radius:50%;background:var(--glow);
    box-shadow:0 0 0 0 var(--glow-soft);animation:glow 2.8s ease-in-out infinite}
  @keyframes glow{0%,100%{box-shadow:0 0 0 0 var(--glow-soft);opacity:.92}
    50%{box-shadow:0 0 0 10px rgba(240,169,59,0);opacity:1}}

  details summary{cursor:pointer;color:var(--muted);font-size:.9rem;list-style:none}
  details summary::-webkit-details-marker{display:none}
  details[open] summary{margin-bottom:10px}

  /* tooltips: CSS-only, driven by data-tip on a small (i) affordance */
  .tip{position:relative;display:inline-flex;align-items:center;justify-content:center;
    width:17px;height:17px;border-radius:50%;border:1px solid var(--line);
    font-family:"Space Mono",monospace;font-size:.62rem;color:var(--muted);
    background:var(--card);cursor:help;vertical-align:middle;user-select:none}
  .tip:hover{border-color:var(--pine);color:var(--pine)}
  .tip::after{
    content:attr(data-tip);position:absolute;left:50%;bottom:calc(100% + 9px);
    transform:translateX(-50%);width:max-content;max-width:250px;
    background:var(--ink);color:#fff;font-family:"Hanken Grotesk",sans-serif;
    font-size:.78rem;font-weight:400;line-height:1.4;letter-spacing:normal;
    text-transform:none;text-align:left;padding:9px 11px;border-radius:10px;
    opacity:0;visibility:hidden;transition:opacity .14s ease;
    pointer-events:none;z-index:20;box-shadow:0 6px 22px rgba(10,67,57,.18)}
  .tip::before{
    content:"";position:absolute;left:50%;bottom:calc(100% + 3px);
    transform:translateX(-50%);border:6px solid transparent;border-top-color:var(--ink);
    opacity:0;visibility:hidden;transition:opacity .14s ease;z-index:20}
  .tip:hover::after,.tip:hover::before{opacity:1;visibility:visible}

  /* settings drawer */
  .scrim{position:fixed;inset:0;background:rgba(10,67,57,.28);
    opacity:0;visibility:hidden;transition:opacity .2s ease;z-index:40}
  .drawer{
    position:fixed;top:0;right:0;height:100%;width:min(420px,92vw);
    background:var(--paper);border-left:1px solid var(--line);
    box-shadow:-18px 0 50px rgba(10,67,57,.16);
    transform:translateX(100%);transition:transform .24s ease;
    z-index:50;overflow-y:auto;padding:24px}
  body.settings-open .scrim{opacity:1;visibility:visible}
  body.settings-open .drawer{transform:translateX(0)}
  .drawer-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
  .drawer-head h2{font-family:"Bricolage Grotesque",sans-serif;font-weight:700;
    font-size:1.3rem;margin:0}
  .drawer .close{border:1px solid var(--line);background:var(--card);
    border-radius:10px;padding:7px 12px;font:inherit;cursor:pointer;color:var(--muted)}
  .drawer .close:hover{background:var(--paper);border-color:var(--muted)}
  .drawer .section{background:var(--card);border:1px solid var(--line);
    border-radius:14px;padding:16px 18px;margin-bottom:14px}
  .drawer .section h3{font-family:"Bricolage Grotesque",sans-serif;font-weight:600;
    font-size:1rem;margin:0 0 4px;display:flex;align-items:center;gap:8px}
  .drawer .section p{color:var(--muted);font-size:.88rem;margin:0 0 12px}
  .drawer .section .now{font-family:"Space Mono",monospace;color:var(--pine-deep)}
  .drawer .full{width:100%;justify-content:center;display:flex}
  .drawer input[name=name]{width:100%;margin-bottom:10px}

  :focus-visible{outline:3px solid var(--glow);outline-offset:2px;border-radius:6px}

  @media (max-width:560px){
    body{padding:18px}
    .top{gap:9px}
    .logo-ph{display:none}
    .item{flex-wrap:wrap}
  }
  @media (prefers-reduced-motion:reduce){
    .status .pulse i{animation:none}
  }
"""

# Friendly, non-technical tooltip copy reused across pages.
_TIPS = {
    "display": "A display is a screen that just shows content — slides, images, "
               "or web pages that a controller sends to it.",
    "controller": "A controller is the device you manage everything from. It holds "
                  "the content and pushes it out to your displays.",
    "pairing": "Pairing links a display to this controller. The display shows a short "
               "code; type that code here (or on the display) to connect them — like "
               "pairing a Bluetooth speaker.",
    "push": "Push sends this controller's current playlist to every paired display, "
            "so they all start showing the latest content.",
    "slides": "In Google Slides choose File → Share → Publish to web, then "
              "paste the link it gives you. That makes a view-only link your displays "
              "can show without anyone signing in.",
    "playlist": "The playlist is the list of things that rotate on screen, in order. "
                "Set how many seconds each item stays up.",
}


def _tip(key: str) -> str:
    """Small (i) affordance with a CSS-only hover tooltip."""
    return f'<span class="tip" data-tip="{_esc(_TIPS[key])}" aria-label="More info">i</span>'


def _settings_drawer(cfg: dict, role: str) -> str:
    """Slide-out Settings drawer: rename, role switch, full-screen link."""
    other = "display" if role == "controller" else "controller"
    other_label = "Display" if other == "display" else "Controller"
    role_label = "Controller" if role == "controller" else "Display"
    other_tip = _TIPS["display"] if other == "display" else _TIPS["controller"]
    return f"""
      <div class="scrim" onclick="toggleSettings()"></div>
      <aside class="drawer" aria-label="Settings">
        <div class="drawer-head">
          <h2>Settings</h2>
          <button class="close" onclick="toggleSettings()">Close</button>
        </div>

        <div class="section">
          <h3>Device name</h3>
          <p>Shown to controllers on the network. This also renames the device itself.</p>
          <form method="post" action="/api/name">
            <input name="name" value="{_esc(cfg['name'])}" placeholder="device name" required>
            <button class="btn-primary full" type="submit">Save name</button>
          </form>
        </div>

        <div class="section">
          <h3>Role <span class="tip" data-tip="{_esc(other_tip)}" aria-label="More info">i</span></h3>
          <p>This device is currently a <span class="now">{role_label}</span>.</p>
          <form method="post" action="/api/role"
                onsubmit="return confirm('Switch this device to {other_label}? You can switch back anytime.');">
            <input type="hidden" name="role" value="{other}">
            <button class="btn-accent full" type="submit">Switch to {other_label}</button>
          </form>
        </div>

        <div class="section">
          <h3>Full-screen view</h3>
          <p>Open the page a screen shows when running as a display.</p>
          <a class="btn-ghost full" href="/screen" style="text-decoration:none">Open full-screen view</a>
        </div>
      </aside>
    """


_SETTINGS_JS = """
  <script>
    function toggleSettings(){document.body.classList.toggle('settings-open');}
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape')document.body.classList.remove('settings-open');
    });
  </script>
"""


def _header(cfg: dict, role: str) -> str:
    """Consistent header: brand, logo placeholder, name chip, role badge, gear."""
    badge_label = "Controller" if role == "controller" else "Display"
    return f"""
      <header class="top">
        <span class="brand"><span class="mark"></span> signage</span>
        <span class="logo-ph" title="Drop your logo here later">Logo</span>
        <span class="spacer"></span>
        <span class="name-chip">{_esc(cfg['name'])}</span>
        <span class="badge {role}">{badge_label}</span>
        <button class="gear" onclick="toggleSettings()">&#9881; Settings</button>
      </header>
    """


def _page(title: str, role: str, cfg: dict, body: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · signage</title>
{_FONTS_HEAD}
<style>{_CSS}</style></head>
<body>
  <main class="stage">
    {_header(cfg, role)}
    {body}
  </main>
  {_settings_drawer(cfg, role)}
  {_SETTINGS_JS}
</body></html>"""
    return HTMLResponse(html)


def _splash(cfg: dict) -> HTMLResponse:
    return HTMLResponse(_SETUP_HTML.replace("__DEVICE_NAME__", cfg["name"]))


def _content_body(cfg: dict) -> str:
    """Content management shared by both roles: add URL/Slides, upload, and the
    playlist with per-item ordering + timing."""
    items = library.list_items()
    if items:
        rows = ""
        last = len(items) - 1
        for n, it in enumerate(items):
            up_dis = " disabled" if n == 0 else ""
            dn_dis = " disabled" if n == last else ""
            rows += f"""
              <div class="item">
                <span class="ord">
                  <form method="post" action="/api/content/move">
                    <input type="hidden" name="item_id" value="{it['id']}">
                    <input type="hidden" name="direction" value="up">
                    <button class="mv" title="Move up" aria-label="Move up"{up_dis}>&#9650;</button>
                  </form>
                  <form method="post" action="/api/content/move">
                    <input type="hidden" name="item_id" value="{it['id']}">
                    <input type="hidden" name="direction" value="down">
                    <button class="mv" title="Move down" aria-label="Move down"{dn_dis}>&#9660;</button>
                  </form>
                </span>
                <span class="name">{_esc(it['name'])}</span>
                <form class="secs" method="post" action="/api/content/seconds">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <input name="seconds" type="number" min="{library.MIN_SECONDS}"
                         value="{it['seconds']}" title="Seconds on screen" aria-label="Seconds on screen">
                  <button class="btn-ghost set" title="Save time">Set</button>
                </form>
                <form method="post" action="/api/content/remove">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <button class="x" title="Remove">&times;</button>
                </form>
              </div>"""
        playlist_inner = rows
    else:
        playlist_inner = ('<p class="empty">Nothing yet — add a link or upload '
                          'something below.</p>')

    shuffle_checked = " checked" if cfg.get("shuffle") else ""
    shuffle_row = f"""
        <form method="post" action="/api/playback" class="shuffle">
          <label><input type="checkbox" name="shuffle" value="on"{shuffle_checked}
            onchange="this.form.submit()"> Shuffle order</label>
          <span class="hint" style="margin:0">Off (default) plays the list top to bottom, in order.</span>
          <noscript><button class="btn-ghost" type="submit">Save</button></noscript>
        </form>"""

    return f"""
      <div class="card">
        <h2>Add a web page or Google Slides {_tip('slides')}</h2>
        <p class="hint">Paste any web address, or a Google Slides
          &ldquo;Publish to web&rdquo; link.</p>
        <form method="post" action="/api/content/url" class="row">
          <input name="url" placeholder="https://… or a Google Slides 'Publish to web' link" required>
          <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="15" title="seconds on screen">
          <button class="btn-primary" type="submit">Add</button>
        </form>
        <p class="hint tail">A Google Slides deck advances by itself and restarts
          from slide&nbsp;1 each time it comes up. Give it enough seconds to play
          all the way through (about your per-slide time &times; the number of
          slides) so the whole deck shows before the next item.</p>
      </div>

      <div class="card">
        <h2>Upload an image or PowerPoint</h2>
        <p class="hint">PowerPoint is converted to slides automatically.</p>
        <form method="post" action="/api/content/upload" enctype="multipart/form-data" class="row">
          <input name="file" type="file" accept="image/*,.pptx,.ppt" required>
          <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="10" title="seconds per image">
          <button class="btn-primary" type="submit">Upload</button>
        </form>
      </div>

      <div class="card">
        <h2>Playlist {_tip('playlist')}</h2>
        <p class="hint">Plays in this order, top to bottom. Use the arrows to
          reorder, and set how long each item stays on screen.</p>
        {shuffle_row}
        {playlist_inner}
      </div>
    """


def _display_home(cfg: dict) -> HTMLResponse:
    code = pairing.current_code()
    if code:
        section = f"""
          <div class="card">
            <h2>Pairing {_tip('pairing')}</h2>
            <p class="hint">On your controller, pick this display and enter this code:</p>
            <div class="code">{_esc(code)}</div>
            <div class="status"><span class="pulse"><i></i></span>
              Waiting to connect · valid for 3 minutes. It also shows on the screen itself.</div>
            <form method="post" action="/api/pair/cancel">
              <button class="btn-danger" type="submit">Cancel pairing</button>
            </form>
          </div>"""
    elif pairing.is_claimed():
        controller = pairing.get_controller() or {}
        section = f"""
          <div class="card">
            <h2>Paired {_tip('pairing')}</h2>
            <p class="hint">Controlled by
              <b>{_esc(controller.get('name') or 'a controller')}</b>.
              Content sent from there will appear on this screen.</p>
            <form method="post" action="/api/pair/start">
              <button class="btn-ghost" type="submit">Re-pair to another controller</button>
            </form>
          </div>"""
    else:
        section = f"""
          <div class="card">
            <h2>Pair to a controller {_tip('pairing')}</h2>
            <p class="hint">Start pairing, then enter the code it shows on your controller.</p>
            <form method="post" action="/api/pair/start">
              <button class="btn-primary" type="submit">Start pairing</button>
            </form>
          </div>"""

    intro = f"""
      <p class="eyebrow">This device shows content {_tip('display')}</p>
      <h1>Display</h1>
      <p class="lead">Pair it to a controller, or add content directly below.</p>
    """
    return _page("Display", "display", cfg, intro + section + _content_body(cfg))


def _control_home(cfg: dict) -> HTMLResponse:
    # Controller's own content + push live ON TOP; paired displays at the BOTTOM.
    push = f"""
      <div class="card">
        <h2>Push to all displays {_tip('push')}</h2>
        <p class="hint">Sends this controller's playlist to every paired display.</p>
        <button class="btn-accent" onclick="pushAll()">Push to all displays</button>
        <div id="pushResult" class="hint tail"></div>
      </div>
      <script>
      async function pushAll(){{
        const box=document.getElementById('pushResult');
        box.textContent='Sending…';
        try{{
          const r=await fetch('/api/push',{{method:'POST'}});
          const d=await r.json();
          if(d.message){{box.textContent=d.message;return;}}
          const ok=d.results.filter(x=>x.ok).length;
          const fail=d.results.filter(x=>!x.ok);
          box.textContent=`Sent to ${{ok}} display(s).`+(fail.length?` Failed: ${{fail.map(f=>f.name).join(', ')}}.`:'');
        }}catch(e){{box.textContent='Push failed.';}}
      }}
      </script>"""

    displays = pairing.list_displays()
    if displays:
        rows = ""
        for d in displays:
            rows += f"""
              <div class="item">
                <span class="name">{_esc(d['name'] or d['device_id'][:8])}</span>
                <span class="meta">{_esc(d['address'])}</span>
                <form method="post" action="/api/displays/remove">
                  <input type="hidden" name="device_id" value="{d['device_id']}">
                  <button class="x" title="Unpair">&times;</button>
                </form>
              </div>"""
        screens_inner = rows
    else:
        screens_inner = ('<p class="empty">No displays paired yet — '
                         'find one below.</p>')

    find = """
      <div class="card">
        <h2>Paired displays</h2>
        <div id="displays">__SCREENS__</div>
      </div>

      <div class="card">
        <h2>Find displays <span class="tip" data-tip="__PAIR_TIP__" aria-label="More info">i</span></h2>
        <p class="hint">Scan the network for displays that are ready to pair.</p>
        <button class="btn-primary" onclick="findDisplays()">Find displays</button>
        <div id="found" class="hint tail">Tap &ldquo;Find displays&rdquo; to scan the network.</div>
        <details style="margin-top:14px">
          <summary>Add by address (fallback)</summary>
          <form method="post" action="/api/displays/add" class="row">
            <input name="address" placeholder="192.168.1.50" required>
            <input name="port" type="number" value="8080">
            <input name="code" placeholder="CODE" required style="width:120px">
            <button class="btn-ghost" type="submit">Pair</button>
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
            <span class="name">${x.name||x.address}</span><span class="meta">${x.address}</span>
            ${x.paired?'<span class="meta">paired</span>':
              `<form method="post" action="/api/displays/add" style="margin:0">
                 <input type="hidden" name="address" value="${x.address}">
                 <input type="hidden" name="port" value="${x.port}">
                 <input name="code" placeholder="CODE" required style="width:110px">
                 <button class="btn-primary" type="submit">Pair</button>
               </form>`}
          </div>`).join('');
        }catch(e){box.textContent='Scan failed.';}
      }
      </script>"""
    find = find.replace("__SCREENS__", screens_inner).replace("__PAIR_TIP__", _esc(_TIPS["pairing"]))

    intro = f"""
      <p class="eyebrow">This device runs the controls {_tip('controller')}</p>
      <h1>Controller</h1>
      <p class="lead">Build your playlist, then push it out to your displays.</p>
    """
    return _page("Controller", "controller", cfg, intro + _content_body(cfg) + push + find)
