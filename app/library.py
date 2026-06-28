# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""The content library — the ordered list of things a screen shows.

Items are stored locally (so a screen keeps playing even if nothing else on the
network is reachable). Each item is a web page/URL, a Google Slides link, or an
image. A PowerPoint is converted to images on add, then stored as image items.
"""

import json
import secrets
import shutil
from pathlib import Path

from . import config, convert

LIBRARY_PATH = config.DATA / "library.json"
ASSETS = config.DATA / "assets"

DEFAULT_URL_SECONDS = 15
DEFAULT_IMAGE_SECONDS = 10


def _load() -> list:
    if LIBRARY_PATH.exists():
        try:
            return json.loads(LIBRARY_PATH.read_text()).get("items", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(items: list) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2))
    tmp.replace(LIBRARY_PATH)


def _append(item: dict) -> dict:
    items = _load()
    items.append(item)
    _save(items)
    return item


def list_items() -> list:
    return _load()


def _is_google_slides(url: str) -> bool:
    return "docs.google.com/presentation" in url


def _slides_autoplay(url: str) -> str:
    """Normalize a 'Publish to web' Slides link for embedding: use the /embed
    form (the /pub full-page view sends X-Frame-Options: SAMEORIGIN and will not
    render inside the screen's iframe), and add the params that make it
    auto-advance and loop on its own."""
    if not _is_google_slides(url):
        return url
    url = url.replace("/pub", "/embed", 1)
    if "start=" not in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "start=true&loop=true&delayms=10000"
    return url


def add_url(url: str, seconds: int = DEFAULT_URL_SECONDS, name: str = "") -> dict:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = _slides_autoplay(url)
    label = name.strip() or ("Google Slides" if _is_google_slides(url) else url)
    return _append({
        "id": secrets.token_hex(4), "type": "url", "ref": url,
        "seconds": int(seconds), "name": label,
    })


def add_image(filename: str, data: bytes, seconds: int = DEFAULT_IMAGE_SECONDS) -> dict:
    ASSETS.mkdir(parents=True, exist_ok=True)
    asset_id = secrets.token_hex(8) + (Path(filename).suffix.lower() or ".img")
    (ASSETS / asset_id).write_bytes(data)
    return _append({
        "id": secrets.token_hex(4), "type": "image", "ref": asset_id,
        "seconds": int(seconds), "name": filename,
    })


def add_pptx(filename: str, data: bytes, seconds: int = DEFAULT_IMAGE_SECONDS) -> list:
    config.WORK.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    work_pptx = config.WORK / (secrets.token_hex(6) + ".pptx")
    work_pptx.write_bytes(data)
    slides_dir = config.WORK / ("slides_" + secrets.token_hex(6))
    try:
        pngs = convert.pptx_to_pngs(work_pptx, slides_dir)
        added = []
        for i, png in enumerate(pngs, 1):
            asset_id = secrets.token_hex(8) + ".png"
            shutil.copyfile(png, ASSETS / asset_id)
            added.append(_append({
                "id": secrets.token_hex(4), "type": "image", "ref": asset_id,
                "seconds": int(seconds), "name": f"{filename} — slide {i}",
            }))
        return added
    finally:
        shutil.rmtree(slides_dir, ignore_errors=True)
        work_pptx.unlink(missing_ok=True)


def remove(item_id: str) -> None:
    _save([i for i in _load() if i["id"] != item_id])


def reorder(order: list) -> None:
    items = _load()
    by_id = {i["id"]: i for i in items}
    kept = set(order)
    new = [by_id[i] for i in order if i in by_id]
    new += [i for i in items if i["id"] not in kept]  # anything not listed stays at the end
    _save(new)


def asset_path(ref: str) -> Path:
    return ASSETS / ref
