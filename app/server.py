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
import secrets
import time
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
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

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
    promote,
    sessions,
    sync,
    updater,
)
from . import __version__ as APP_VERSION

_SETUP_HTML = (Path(__file__).parent / "pages" / "setup.html").read_text(encoding="utf-8")
_SCREEN_HTML = (Path(__file__).parent / "pages" / "screen.html").read_text(encoding="utf-8")
_STATIC_DIR = Path(__file__).parent / "static"  # locally-vendored fonts, etc.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB ceiling for any single upload


def create_app() -> FastAPI:
    device_id = identity.get_or_create_device_id()
    _started = time.time()

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

    # Serve locally-vendored assets (fonts) so the offline device never reaches
    # out to a CDN. Created defensively in case the folder is somehow absent.
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- optional admin login gate -----------------------------------------
    # Open by default: with no password set the panel behaves exactly as before.
    # Once an admin sets a password, the human/admin routes require a session.
    # Machine + kiosk routes stay open: the signed pairing/push endpoints, the
    # screen and its data, image assets (a display fetches these during a push),
    # the static files, the QR, and the login page itself.
    _OPEN_EXACT = {"/healthz", "/screen", "/api/screen-data", "/login", "/logout",
                   "/qr.png", "/favicon.ico"}
    _OPEN_PREFIX = ("/asset/", "/recv-asset/", "/static/", "/api/pair/claim", "/api/playlist")
    _login_fails = {}  # client ip -> (count, window_start): basic brute-force throttle

    def _throttled(ip: str) -> bool:
        rec = _login_fails.get(ip)
        if not rec:
            return False
        count, start = rec
        if time.time() - start > 300:
            _login_fails.pop(ip, None)
            return False
        return count >= 8

    def _note_fail(ip: str) -> None:
        count, start = _login_fails.get(ip, (0, time.time()))
        if time.time() - start > 300:
            count, start = 0, time.time()
        _login_fails[ip] = (count + 1, start)

    def _authed(request: Request) -> bool:
        return sessions.valid(request.cookies.get(sessions.COOKIE_NAME))

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if path in _OPEN_EXACT or any(path.startswith(p) for p in _OPEN_PREFIX):
            return await call_next(request)
        if not auth.has_credentials():        # no password set → open access
            return await call_next(request)
        if _authed(request):
            return await call_next(request)
        # Browser navigation (any non-API GET) goes to the login page; API calls
        # and form posts get a clean 401 so callers don't mistake HTML for data.
        if request.method == "GET" and not path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "login required"}, status_code=401)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if not auth.has_credentials() or _authed(request):
            return RedirectResponse("/", status_code=303)
        return _login_page(current())

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = request.client.host if request.client else "?"
        if _throttled(ip):
            return _login_page(current(), "Too many attempts. Wait a few minutes, then try again.")
        if auth.verify(username.strip(), password):
            _login_fails.pop(ip, None)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(sessions.COOKIE_NAME, sessions.create(),
                            httponly=True, samesite="lax", max_age=sessions.TTL, path="/")
            activity.log("Signed in to the control panel")
            return resp
        _note_fail(ip)
        activity.log("A sign-in attempt failed")
        return _login_page(current(), "That username or password didn't match.")

    @app.post("/logout")
    def logout(request: Request):
        sessions.destroy(request.cookies.get(sessions.COOKIE_NAME))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(sessions.COOKIE_NAME, path="/")
        return resp

    @app.post("/api/password/set")
    def password_set(request: Request, username: str = Form(...), password: str = Form(...)):
        # The middleware already gates this: setting the FIRST password is open
        # (like first-run setup); CHANGING one requires a live session. So the
        # caller is authorized by the time we get here.
        if len(password) < 6:
            return JSONResponse({"error": "Password must be at least 6 characters."}, status_code=400)
        first = not auth.has_credentials()
        auth.set_credentials(username.strip() or "admin", password)
        if not first:
            sessions.clear_all()  # a password change evicts every other session
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(sessions.COOKIE_NAME, sessions.create(),
                        httponly=True, samesite="lax", max_age=sessions.TTL, path="/")
        activity.log("Set the control-panel password" if first else "Changed the control-panel password")
        return resp

    @app.post("/api/password/clear")
    def password_clear(request: Request):
        # Reachable only with a live session (the middleware gates it).
        auth.clear_credentials()
        sessions.clear_all()
        activity.log("Removed the control-panel password — the panel is open again")
        return RedirectResponse("/", status_code=303)

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
            return _control_home(cfg, device_id)
        return _display_home(cfg)

    @app.get("/health", response_class=HTMLResponse)
    def health():
        cfg = current()
        role = cfg.get("role") or "display"
        return _page("Health", role, cfg, _health_body(cfg, role, device_id, _started))

    @app.post("/api/role")
    def set_role(role: str = Form(...)):
        if role not in config.VALID_ROLES:
            return JSONResponse({"error": "invalid role"}, status_code=400)
        cfg = current()
        cfg["role"] = role
        config.save_config(cfg)
        _readvertise()
        activity.log(f"Switched this device's role to {role}")
        # A new controller needs LibreOffice/poppler to convert PowerPoint. The
        # app is sandboxed, so ask the privileged helper to install them.
        if role == "controller" and not promote.has_conversion_tools():
            promote.request_promotion()
            activity.log("Requested install of PowerPoint conversion packages")
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
            for item in sync.items_for_display(library.list_items(), device_id):
                if item["type"] == "url":
                    items.append({"type": "url", "src": item["ref"], "seconds": item["seconds"]})
                elif item["type"] == "slideshow":
                    items.append({"type": "slideshow", "seconds": item["seconds"],
                                  "srcs": [f"/asset/{r}" for r in item.get("refs", [])]})
                elif item["type"] == "video":
                    src = item["ref"] if item["ref"].startswith("http") else f"/asset/{item['ref']}"
                    items.append({"type": "video", "src": src, "seconds": item["seconds"]})
                else:
                    items.append({"type": "image", "src": f"/asset/{item['ref']}", "seconds": item["seconds"]})
        ips = discovery.lan_ips()
        primary = ips[0] if ips else "127.0.0.1"
        return {
            "items": items,
            "pairing_code": pairing.current_code(),
            "shuffle": bool(current().get("shuffle")),
            # where to reach this device: the primary address, plus every interface
            # so a wired + Wi-Fi box always shows an address you can actually use.
            "connect_url": f"http://{primary}:{config.PORT}",
            "ips": ips,
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
        The encoded address is always this device's own — never client input.
        Any failure (missing qrcode/Pillow, render error) degrades to 404 so the
        screen simply hides the QR rather than erroring."""
        try:
            import qrcode  # local import: a missing optional dep must not block boot
            url = f"http://{discovery.primary_ip()}:{config.PORT}"
            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
        except Exception:
            return Response(status_code=404)

    # --- content ------------------------------------------------------------

    @app.post("/api/content/url")
    def add_url(url: str = Form(...), seconds: int = Form(library.DEFAULT_URL_SECONDS)):
        item = library.add_url(url, seconds)
        library.measure_slides(item["id"])  # size a Slides deck to play all the way through
        activity.log("Added a link to the playlist", url)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/content/upload")
    async def add_upload(file: UploadFile, seconds: int = Form(library.DEFAULT_IMAGE_SECONDS)):
        name = file.filename or "upload"
        low = name.lower()
        # Stream the upload to a temp file in chunks so a large video is never held
        # whole in memory (a small controller would run out of RAM). Conversion +
        # disk work runs off the event loop.
        config.WORK.mkdir(parents=True, exist_ok=True)
        tmp = config.WORK / ("upload_" + secrets.token_hex(8) + (Path(name).suffix.lower() or ".bin"))
        try:
            total = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise ValueError("file too large")
                    out.write(chunk)
            if low.endswith((".pptx", ".ppt")):
                item = await run_in_threadpool(library.add_pptx, name, tmp.read_bytes(), seconds)
                activity.log(f"Added a PowerPoint ({item.get('slides', 0)} slide(s))", name)
            elif low.endswith(library.VIDEO_EXTS):
                await run_in_threadpool(library.add_video_path, name, str(tmp))
                tmp = None  # moved into assets; nothing to clean up
                activity.log("Added a video to the playlist", name)
            else:
                await run_in_threadpool(library.add_image, name, tmp.read_bytes(), seconds)
                activity.log("Added an image to the playlist", name)
        except ValueError:
            return JSONResponse({"error": "That file is too large (1 GB max)."}, status_code=413)
        except RuntimeError as exc:
            activity.log("An upload could not be processed", str(exc))
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            if tmp is not None:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass
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

    @app.post("/api/content/targets")
    def content_targets(item_id: str = Form(...), display: list[str] = Form(default=[])):
        # No displays checked = show on every screen; otherwise just the chosen ones.
        library.set_targets(item_id, display)
        activity.log("Changed which screens an item shows on")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/playback")
    def set_playback(shuffle: str = Form(default="")):
        cfg = current()
        cfg["shuffle"] = bool(shuffle)
        config.save_config(cfg)
        activity.log("Shuffle turned " + ("on" if cfg["shuffle"] else "off"))
        return RedirectResponse("/", status_code=303)

    @app.post("/api/update")
    def do_update():
        updater.request_update()
        activity.log("Started a software update")
        return RedirectResponse("/", status_code=303)

    @app.post("/api/promote")
    def promote_now():
        # Manual (re)trigger of the conversion-package install from the UI.
        if not promote.has_conversion_tools():
            promote.request_promotion()
            activity.log("Requested install of PowerPoint conversion packages")
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
        # receive() verifies the signature then downloads assets; run it off the
        # event loop so a slow or large push can never block the kiosk web server.
        ok = await run_in_threadpool(
            sync.receive, body.get("manifest"), body.get("signature"), controller["site_key"]
        )
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
        # Best-effort: (re)measure any Google Slides deck we couldn't size yet, so a
        # deck added on a slow/flaky connection still gets its full-deck timing.
        for it in library.list_items():
            if it["type"] == "url" and not it.get("slides") and library._is_google_slides(it.get("ref", "")):
                library.measure_slides(it["id"])
        base_url = f"http://{discovery.primary_ip()}:{config.PORT}"
        # Each display gets only the items aimed at it (untargeted items go to all).
        results = sync.push_targeted(
            displays, library.list_items(), current()["name"], base_url, auth.get_or_create_site_key()
        )
        ok = sum(1 for r in results if r.get("ok"))
        activity.log(f"Pushed content to {ok} of {len(results)} display(s)")
        return {"results": results}

    return app


# --- built-in HTML pages (styled to match pages/setup.html design language) ---

def _esc(text) -> str:
    return _htmllib.escape(str(text))


# Fonts are vendored locally (app/static/fonts) so the offline device never
# depends on the Google Fonts CDN. Licensing: app/static/fonts/OFL.txt.
_FONTS_HEAD = '<link rel="stylesheet" href="/static/fonts/fonts.css">'

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
  .card.note{border-left:4px solid var(--glow)}
  .card.note.warn{border-left-color:#b42318}
  .card.note h2{font-size:1.02rem;margin-bottom:6px}

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
  .item .name{flex:1;min-width:0;font-weight:500;display:flex;flex-direction:column;justify-content:center}
  .item .name .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .slidenote{font-family:"Space Mono",monospace;font-size:.7rem;color:var(--muted);margin-top:2px}
  .secs.auto{font-family:"Space Mono",monospace;font-size:.72rem;color:var(--muted);align-self:center;padding:8px 10px}
  /* per-item 'which screens' control */
  .tgt{margin-top:3px}
  .tgt>summary{font-family:"Space Mono",monospace;font-size:.7rem;color:var(--muted);cursor:pointer;list-style:none}
  .tgt>summary::-webkit-details-marker{display:none}
  .tgt[open]>summary{color:var(--pine)}
  .tgt form{margin:6px 0 2px;display:flex;flex-direction:column;gap:4px}
  .tgt .thint{margin:0;color:var(--muted);font-size:.7rem}
  .tgtbox{display:flex;align-items:center;gap:6px;font-size:.82rem;font-weight:400;cursor:pointer}
  .tgtbox input{width:15px;height:15px;accent-color:var(--pine)}
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
  /* health screen rows */
  .hrow{display:flex;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
  .hrow:first-of-type{border-top:0}
  .hk{flex:none;width:150px;color:var(--muted);font-size:.9rem}
  .hv{flex:1;min-width:0;word-break:break-word}
  .mono{font-family:"Space Mono",monospace;font-size:.85em}
  .muted{color:var(--muted)}
  .ok-dot{display:inline-block;width:9px;height:9px;border-radius:50%;
          background:#1a8f4c;margin-right:5px}

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
  .drawer .section input{width:100%;margin-bottom:10px}

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
    "password": "Optional lock. Set a username and password and the panel asks for "
                "them before anyone can change content or settings. Your screens "
                "keep playing without it.",
}


def _tip(key: str) -> str:
    """Small (i) affordance with a CSS-only hover tooltip."""
    return f'<span class="tip" data-tip="{_esc(_TIPS[key])}" aria-label="More info">i</span>'


def _settings_drawer(cfg: dict, role: str) -> str:
    """Slide-out Settings drawer: rename, role switch, password lock, screen link."""
    other = "display" if role == "controller" else "controller"
    other_label = "Display" if other == "display" else "Controller"
    role_label = "Controller" if role == "controller" else "Display"
    other_tip = _TIPS["display"] if other == "display" else _TIPS["controller"]

    if auth.has_credentials():
        uname = _esc(auth.admin_username() or "admin")
        pw_section = f"""
        <div class="section">
          <h3>Password {_tip('password')}</h3>
          <p>This panel is <span class="now">locked</span>. Only someone with the
            password can change content or settings.</p>
          <details>
            <summary>Change password</summary>
            <form method="post" action="/api/password/set" style="margin-top:10px">
              <input name="username" value="{uname}" placeholder="username" required>
              <input name="password" type="password" placeholder="new password (min 6)" required>
              <button class="btn-primary full" type="submit">Save new password</button>
            </form>
          </details>
          <form method="post" action="/api/password/clear" style="margin-top:10px"
                onsubmit="return confirm('Remove the password? The panel will be open to anyone on your network.');">
            <button class="btn-danger full" type="submit">Remove password (make open)</button>
          </form>
          <form method="post" action="/logout" style="margin-top:10px">
            <button class="btn-ghost full" type="submit">Log out</button>
          </form>
        </div>"""
    else:
        pw_section = f"""
        <div class="section">
          <h3>Password {_tip('password')}</h3>
          <p>Optional, but recommended. Lock this panel so only people with the
            password can change content or settings — your screens keep playing
            either way.</p>
          <form method="post" action="/api/password/set">
            <input name="username" value="admin" placeholder="username" required>
            <input name="password" type="password" placeholder="password (min 6 characters)" required>
            <button class="btn-accent full" type="submit">Lock this panel</button>
          </form>
        </div>"""

    upd = updater.status()
    upd_line = (f'<p>Last update: <span class="now">{_esc(upd["state"])}</span> &mdash; '
                f'{_esc(upd.get("detail", ""))}</p>') if upd.get("state") else ""
    software_section = f"""
        <div class="section">
          <h3>Software</h3>
          <p>Version <span class="now">{_esc(updater.current_version())}</span>. Updates are
            staged and health-checked &mdash; if a new version doesn't start, the device
            rolls back to the previous one automatically.</p>
          {upd_line}
          <form method="post" action="/api/update"
                onsubmit="return confirm('Update to the latest version? The screen restarts briefly.');">
            <button class="btn-accent full" type="submit">Update now</button>
          </form>
        </div>"""

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

        {pw_section}

        <div class="section">
          <h3>Health &amp; activity</h3>
          <p>See device status and a log of recent changes.</p>
          <a class="btn-ghost full" href="/health" style="text-decoration:none">Open health screen</a>
        </div>

        {software_section}

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


def _login_page(cfg: dict, error: str = "") -> HTMLResponse:
    """The sign-in screen shown when the panel is locked. Includes the offline
    recovery command so a forgotten password is never a dead end."""
    err = f'<p class="loginerr">{_esc(error)}</p>' if error else ""
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · signage</title>
{_FONTS_HEAD}
<style>{_CSS}
  .loginwrap{{min-height:78vh;display:flex;align-items:center;justify-content:center}}
  .logincard{{width:100%;max-width:380px;margin:0}}
  .logincard .fld{{margin-bottom:10px}}
  .logincard .fld input{{width:100%}}
  .loginerr{{background:#fdecea;border:1px solid #f1c9c4;color:#b42318;
             padding:10px 12px;border-radius:10px;font-size:.9rem;margin:0 0 12px}}
  .full{{width:100%;justify-content:center;display:flex}}
  .cmd{{background:var(--ink);color:#fff;padding:10px 12px;border-radius:10px;
        font-family:"Space Mono",monospace;font-size:.72rem;white-space:pre-wrap;
        word-break:break-all;margin:8px 0 0}}
</style></head>
<body>
  <main class="stage">
    <div class="loginwrap">
      <div class="card logincard">
        <span class="brand"><span class="mark"></span> signage</span>
        <h1 style="margin:14px 0 4px">Sign in</h1>
        <p class="lead">Enter the control-panel password for
          <b>{_esc(cfg['name'])}</b>.</p>
        {err}
        <form method="post" action="/login">
          <label class="fld">Username
            <input name="username" autocomplete="username" required autofocus>
          </label>
          <label class="fld">Password
            <input name="password" type="password" autocomplete="current-password" required>
          </label>
          <button class="btn-primary full" type="submit">Sign in</button>
        </form>
        <details style="margin-top:16px">
          <summary>Forgot the password?</summary>
          <p class="hint" style="margin-top:8px">On the device itself (keyboard or
            SSH), run this once to remove the password and reopen the panel:</p>
          <pre class="cmd">sudo -u signage /opt/signage/.venv/bin/python -m app reset-password</pre>
        </details>
      </div>
    </div>
  </main>
</body></html>"""
    return HTMLResponse(html)


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _health_body(cfg: dict, role: str, device_id: str, started: float) -> str:
    """Plain-language status + the recent activity log for this device."""
    url = f"http://{discovery.primary_ip()}:{config.PORT}"
    rows = []

    def row(key, value):
        rows.append(f'<div class="hrow"><span class="hk">{_esc(key)}</span>'
                    f'<span class="hv">{value}</span></div>')

    row("Status", '<span class="ok-dot"></span> Running')
    row("Name", _esc(cfg.get("name") or "—"))
    row("Role", _esc((role or "unset").capitalize()))
    row("Device ID", f'<span class="mono">{_esc(device_id[:12])}…</span>')
    row("Address", f'<a href="{_esc(url)}">{_esc(url)}</a>')
    row("App version", _esc(APP_VERSION))
    row("Uptime", _esc(_fmt_uptime(time.time() - started)))

    if role == "controller":
        row("Paired displays", str(len(pairing.list_displays())))
        row("Playlist items", str(len(library.list_items())))
        row("PowerPoint support", _esc(promote.conversion_state()["state"]))
    else:
        pushed = sync.screen_items()
        if pushed is not None:
            row("Now showing", f"{len(pushed)} item(s) sent from a controller")
        else:
            n = len(library.list_items())
            row("Now showing", f"{n} local item(s)" if n else "nothing yet")
        ctrl = pairing.get_controller() if pairing.is_claimed() else None
        row("Paired to", _esc(ctrl.get("name") or "a controller") if ctrl else "not paired")

    status_card = '<div class="card"><h2>This device</h2>' + "".join(rows) + "</div>"

    events = activity.recent(40)
    if events:
        log_rows = "".join(
            '<div class="hrow"><span class="hk mono">' + _esc(e.get("when", "")) + "</span>"
            '<span class="hv">' + _esc(e.get("event", ""))
            + (f' <span class="muted">— {_esc(e.get("detail"))}</span>' if e.get("detail") else "")
            + "</span></div>"
            for e in events
        )
    else:
        log_rows = '<p class="empty">No activity yet.</p>'
    log_card = '<div class="card"><h2>Recent activity</h2>' + log_rows + "</div>"

    intro = ('<p class="eyebrow">Status &amp; activity</p><h1>Health</h1>'
             '<p class="lead">A quick look at what this device is doing.</p>')
    return intro + status_card + log_card


def _targets_control(it: dict, displays) -> str:
    """Per-item 'which screens' control, shown on a controller with paired displays.
    No box checked = every screen; otherwise just the chosen displays."""
    if not displays:
        return ""
    tgt = it.get("targets") or []
    boxes = ""
    for d in displays:
        did = d["device_id"]
        chk = " checked" if did in tgt else ""
        boxes += (f'<label class="tgtbox"><input type="checkbox" name="display" value="{_esc(did)}"'
                  f'{chk} onchange="this.form.submit()"> {_esc(d.get("name") or did[:8])}</label>')
    summ = "All screens" if not tgt else f"{len(tgt)} screen(s)"
    return (f'<details class="tgt"><summary>On: {summ}</summary>'
            f'<form method="post" action="/api/content/targets">'
            f'<input type="hidden" name="item_id" value="{it["id"]}">'
            f'<p class="thint">Leave all unchecked to show on every screen.</p>'
            f'{boxes}</form></details>')


def _content_body(cfg: dict, displays=None) -> str:
    """Content management shared by both roles: add URL/Slides, upload, and the
    playlist with per-item ordering, timing, and (on a controller) which screens."""
    items = library.list_items()
    if items:
        rows = ""
        last = len(items) - 1
        for n, it in enumerate(items):
            up_dis = " disabled" if n == 0 else ""
            dn_dis = " disabled" if n == last else ""
            is_gslides = it["type"] == "url" and it.get("slides")
            is_show = it["type"] == "slideshow"
            is_video = it["type"] == "video"
            is_slides_raw = (it["type"] == "url" and not it.get("slides")
                             and library._is_google_slides(it.get("ref", "")))
            secs_form = f"""
                <form class="secs" method="post" action="/api/content/seconds">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="{it['seconds']}"
                         title="{{title}}" aria-label="{{title}}">
                  <button class="btn-ghost set" title="Save time">Set</button>
                </form>"""
            if is_gslides:
                note = (f'<span class="slidenote">&#9654; plays all {it["slides"]} slides &middot; '
                        f'{it.get("per_slide", "?")}s each (from the link)</span>')
                secs_html = '<span class="secs auto" title="Timed automatically from the deck">auto</span>'
            elif is_show:
                note = f'<span class="slidenote">&#9654; {it["slides"]}-slide show &middot; {it["seconds"]}s each</span>'
                secs_html = secs_form.replace("{title}", "Seconds per slide")
            elif is_video:
                note = '<span class="slidenote">&#9654; video' + ('' if it["seconds"] else ' (plays in full)') + '</span>'
                secs_html = f"""
                <form class="secs" method="post" action="/api/content/seconds">
                  <input type="hidden" name="item_id" value="{it['id']}">
                  <input name="seconds" type="number" min="0" value="{it['seconds']}"
                         title="Seconds (0 = play the whole video)" aria-label="Seconds (0 = play the whole video)">
                  <button class="btn-ghost set" title="Save time">Set</button>
                </form>"""
            elif is_slides_raw:
                note = ('<span class="slidenote">&#9654; Google Slides &mdash; couldn\'t read its '
                        'length; set the seconds to cover the whole deck</span>')
                secs_html = secs_form.replace("{title}", "Seconds (cover the whole deck)")
            else:
                note = ""
                secs_html = secs_form.replace("{title}", "Seconds on screen")
            targets_html = _targets_control(it, displays)
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
                <span class="name"><span class="nm">{_esc(it['name'])}</span>{note}{targets_html}</span>
                {secs_html}
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
        <h2>Add a web page, Google Slides, or video {_tip('slides')}</h2>
        <p class="hint">Paste a web address, a Google Slides &ldquo;Publish to web&rdquo;
          link, a YouTube link, or a direct video link.</p>
        <form method="post" action="/api/content/url" class="row">
          <input name="url" placeholder="https://…  ·  Google Slides link  ·  YouTube link" required>
          <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="15" title="seconds on screen">
          <button class="btn-primary" type="submit">Add</button>
        </form>
        <p class="hint tail">A Google Slides deck plays all the way through
          automatically: it reads the slide count and the per-slide time from your
          &ldquo;Publish to web&rdquo; link (the device needs internet the moment you
          add it). Turn on auto-advance when you publish so the link carries the timing.</p>
      </div>

      <div class="card">
        <h2>Upload an image, PowerPoint, or video</h2>
        <p class="hint">A PowerPoint becomes <b>one</b> slideshow that plays all its
          slides in order; a video plays in full. The seconds box is how long each
          image or slide stays up.</p>
        <form method="post" action="/api/content/upload" enctype="multipart/form-data" class="row">
          <input name="file" type="file" accept="image/*,video/*,.pptx,.ppt" required>
          <input name="seconds" type="number" min="{library.MIN_SECONDS}" value="10" title="seconds per image or slide">
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


def _promote_banner(conv: dict) -> str:
    """A status note on the controller home about PowerPoint-conversion support,
    shown only when it isn't ready yet."""
    state = conv.get("state")
    if state == "ready":
        return ""
    detail = _esc(conv.get("detail", ""))
    if state == "installing":
        return (f'<div class="card note"><h2>Setting up PowerPoint support&hellip;</h2>'
                f'<p class="hint">{detail} Google Slides, web pages, and images work now.</p></div>')
    if state == "failed":
        return (f'<div class="card note warn"><h2>PowerPoint support didn\'t install</h2>'
                f'<p class="hint">{detail}</p>'
                f'<form method="post" action="/api/promote"><button class="btn-accent" type="submit">Try again</button></form></div>')
    return (f'<div class="card note"><h2>PowerPoint support not installed</h2>'
            f'<p class="hint">{detail} You can still use Google Slides, web pages, and images.</p>'
            f'<form method="post" action="/api/promote"><button class="btn-accent" type="submit">Install PowerPoint support</button></form></div>')


def _control_home(cfg: dict, device_id: str = "") -> HTMLResponse:
    # Controller's own content + push live ON TOP; paired displays at the BOTTOM.
    banner = _promote_banner(promote.conversion_state())
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
    # Targeting offers this controller's own screen plus every paired display
    # (only worth showing once there's at least one display to choose between).
    targetable = ([{"device_id": device_id, "name": cfg["name"] + " · this screen"}] + displays) if displays else []
    return _page("Controller", "controller", cfg, intro + banner + _content_body(cfg, targetable) + push + find)
