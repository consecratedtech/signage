# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""Pushing content from a controller to its displays.

The controller builds a manifest of its playlist, signs it with the site key,
and sends it to each paired display. A display verifies the signature against
the controller it trusts, downloads any images, and caches everything locally —
so it keeps playing even if the controller later goes offline.
"""

import hashlib
import json
import urllib.request
from pathlib import Path

from . import commands, config

RECEIVED_PATH = config.DATA / "received.json"
RECV_ASSETS = config.DATA / "recv_assets"


def canon(manifest: dict) -> bytes:
    """Stable byte form both sides sign/verify identically."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def post_json(url: str, obj: dict, timeout: int = 10) -> dict:
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# --- controller side --------------------------------------------------------

def build_manifest(items: list, from_name: str, base_url: str) -> dict:
    """Turn the controller's library into a manifest the display can play.
    Images are referenced by a URL back to the controller so the display can
    fetch and cache them."""
    out = []
    for it in items:
        if it["type"] == "url":
            out.append({"type": "url", "seconds": it["seconds"], "url": it["ref"]})
        elif it["type"] == "slideshow":
            out.append({
                "type": "slideshow", "seconds": it["seconds"],
                "asset_urls": [f"{base_url}/asset/{r}" for r in it.get("refs", [])],
            })
        elif it["type"] == "video":
            if it["ref"].startswith("http"):  # a direct video link plays as-is
                out.append({"type": "video", "seconds": it["seconds"], "url": it["ref"]})
            else:
                out.append({"type": "video", "seconds": it["seconds"],
                            "asset_url": f"{base_url}/asset/{it['ref']}"})
        else:
            out.append({
                "type": "image", "seconds": it["seconds"],
                "asset_url": f"{base_url}/asset/{it['ref']}",
            })
    return {"items": out, "from": from_name}


def push_all(displays: list, manifest: dict, site_key: str) -> list:
    signature = commands.sign(site_key, canon(manifest))
    results = []
    for d in displays:
        url = f"http://{d['address']}:{d['port']}/api/playlist"
        try:
            post_json(url, {"manifest": manifest, "signature": signature})
            results.append({"name": d.get("name") or d["device_id"][:8], "ok": True})
        except Exception as exc:
            results.append({"name": d.get("name") or d["device_id"][:8], "ok": False, "error": str(exc)})
    return results


# --- display side -----------------------------------------------------------

def _save(playlist: dict) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = RECEIVED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(playlist, indent=2))
    tmp.replace(RECEIVED_PATH)


def receive(manifest: dict, signature: str, controller_site_key: str) -> bool:
    """Verify the manifest came from our controller, then cache it + its images."""
    if not manifest or not signature:
        return False
    if not commands.verify(controller_site_key, canon(manifest), signature):
        return False

    RECV_ASSETS.mkdir(parents=True, exist_ok=True)
    items = []
    for it in manifest.get("items", []):
        if it.get("type") == "url":
            items.append({"type": "url", "src": it["url"], "seconds": it["seconds"]})
        elif it.get("type") == "slideshow":
            srcs = []
            for asset_url in it.get("asset_urls", []):
                name = hashlib.sha256(asset_url.encode()).hexdigest()[:16] + ".img"
                try:
                    urllib.request.urlretrieve(asset_url, str(RECV_ASSETS / name))
                except Exception:
                    continue  # skip a slide we couldn't fetch; keep the rest
                srcs.append(f"/recv-asset/{name}")
            if srcs:
                items.append({"type": "slideshow", "srcs": srcs, "seconds": it["seconds"]})
        elif it.get("type") == "video":
            if it.get("url"):
                items.append({"type": "video", "src": it["url"], "seconds": it["seconds"]})
            elif it.get("asset_url"):
                ext = Path(it["asset_url"].split("?")[0]).suffix or ".mp4"
                name = hashlib.sha256(it["asset_url"].encode()).hexdigest()[:16] + ext
                try:
                    urllib.request.urlretrieve(it["asset_url"], str(RECV_ASSETS / name))
                except Exception:
                    continue  # skip a video we couldn't fetch; keep the rest
                items.append({"type": "video", "src": f"/recv-asset/{name}", "seconds": it["seconds"]})
        else:
            name = hashlib.sha256(it["asset_url"].encode()).hexdigest()[:16] + ".img"
            try:
                urllib.request.urlretrieve(it["asset_url"], str(RECV_ASSETS / name))
            except Exception:
                continue  # skip an image we couldn't fetch; keep the rest playing
            items.append({"type": "image", "src": f"/recv-asset/{name}", "seconds": it["seconds"]})
    _save({"items": items})
    return True


def screen_items():
    """The pushed playlist for this display, or None if nothing was pushed."""
    if not RECEIVED_PATH.exists():
        return None
    try:
        return json.loads(RECEIVED_PATH.read_text()).get("items", [])
    except (json.JSONDecodeError, OSError):
        return None


def recv_asset_path(name: str) -> Path:
    return RECV_ASSETS / name
