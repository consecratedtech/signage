# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""The content library — the ordered list of things a screen shows.

Items are stored locally (so a screen keeps playing even if nothing else on the
network is reachable). Each item is a web page/URL, a Google Slides link, or an
image. A PowerPoint is converted to images on add, then stored as image items.
"""

import json
import re
import secrets
import shutil
import urllib.request
from pathlib import Path

from . import config, convert

LIBRARY_PATH = config.DATA / "library.json"
ASSETS = config.DATA / "assets"

DEFAULT_URL_SECONDS = 15
DEFAULT_IMAGE_SECONDS = 10
MIN_SECONDS = 3  # never let an item flash by faster than this


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
    """Normalize a 'Publish to web' Slides link for embedding: use the /embed form
    (the /pub full-page view sends X-Frame-Options: SAMEORIGIN and won't render in
    an iframe), ensure it auto-advances, and force loop=false so the deck holds on
    its LAST slide at the end instead of cycling back to slide 1 — the screen
    restarts it at slide 1 each cycle and times it to show every slide."""
    if not _is_google_slides(url):
        return url
    url = url.replace("/pub", "/embed", 1)
    if "start=" not in url:
        sep = "&" if "?" in url else "?"
        url = url + sep + "start=true&delayms=10000"
    if "loop=" in url:
        url = re.sub(r"loop=(?:true|false|1|0)", "loop=false", url)
    else:
        url = url + ("&" if "?" in url else "?") + "loop=false"
    return url


_SLIDES_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _slides_per_slide_ms(url: str) -> int:
    """The per-slide auto-advance time baked into a published Slides URL."""
    m = re.search(r"delayms=(\d+)", url)
    return int(m.group(1)) if m else 0


def _count_slides(url: str) -> int:
    """Best-effort count of slides in a published Google Slides deck: fetch the
    published page and count the slide entries in its embedded model. Returns 0
    on any failure (offline, markup change) so callers fall back gracefully."""
    try:
        fetch = url.replace("/embed", "/pub", 1)
        req = urllib.request.Request(fetch, headers={"User-Agent": _SLIDES_UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read(8_000_000).decode("utf-8", "replace")
        # Each slide is an array ["gID_0_N",<index>,"title",...] in the model.
        n = len(re.findall(r'\["g[0-9a-z]+_\d+_\d+",\d+,"', html))
        return n if 1 <= n <= 500 else 0
    except Exception:
        return 0


def _slides_plan(url: str):
    """(slide_count, per_slide_seconds) for an auto-advancing published deck, or
    (0, 0) when it can't be determined (no delayms / fetch / parse failure)."""
    ms = _slides_per_slide_ms(url)
    if ms <= 0:
        return 0, 0
    n = _count_slides(url)
    if n <= 0:
        return 0, 0
    return n, max(ms // 1000, 1)


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


def measure_slides(item_id: str) -> dict:
    """Network step, run right after adding a URL: for a Google Slides item, size
    its on-screen time to the whole deck — slide count x the per-slide delay from
    the link — so the deck plays all the way through before the next item instead
    of being cut off after one slide. No-op for non-Slides items or when the deck
    can't be measured (offline, markup change). Kept out of add_url so the
    library core stays pure and offline-testable."""
    items = _load()
    for it in items:
        if it["id"] == item_id and it.get("type") == "url" and _is_google_slides(it.get("ref", "")):
            n, per = _slides_plan(it["ref"])
            if n and per:
                it["slides"] = n
                it["per_slide"] = per
                # Size to show EVERY slide: deck time + a buffer for the initial
                # load and Google's own timing drift. With loop=false the buffer
                # just lingers on the last slide, so it's never cut off.
                it["seconds"] = (n + 1) * per + 15
                _save(items)
            return it
    return {}


def add_image(filename: str, data: bytes, seconds: int = DEFAULT_IMAGE_SECONDS) -> dict:
    ASSETS.mkdir(parents=True, exist_ok=True)
    asset_id = secrets.token_hex(8) + (Path(filename).suffix.lower() or ".img")
    (ASSETS / asset_id).write_bytes(data)
    return _append({
        "id": secrets.token_hex(4), "type": "image", "ref": asset_id,
        "seconds": int(seconds), "name": filename,
    })


def add_pptx(filename: str, data: bytes, seconds: int = DEFAULT_IMAGE_SECONDS) -> dict:
    """Convert a PowerPoint to images and add it as ONE slideshow item that plays
    through all its slides in order (each for `seconds`) before the next playlist
    item — not one row per slide."""
    config.WORK.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    work_pptx = config.WORK / (secrets.token_hex(6) + ".pptx")
    work_pptx.write_bytes(data)
    slides_dir = config.WORK / ("slides_" + secrets.token_hex(6))
    try:
        pngs = convert.pptx_to_pngs(work_pptx, slides_dir)
        refs = []
        for png in pngs:
            asset_id = secrets.token_hex(8) + ".png"
            shutil.copyfile(png, ASSETS / asset_id)
            refs.append(asset_id)
        return _append({
            "id": secrets.token_hex(4), "type": "slideshow", "refs": refs,
            "seconds": int(seconds), "name": filename, "slides": len(refs),
        })
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


def move(item_id: str, direction: str) -> None:
    """Nudge one item up or down a single slot. Lets an operator fix the play
    order from the panel without removing and re-adding the item."""
    items = _load()
    idx = next((n for n, it in enumerate(items) if it["id"] == item_id), None)
    if idx is None:
        return
    swap = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap < len(items):
        items[idx], items[swap] = items[swap], items[idx]
        _save(items)


def set_seconds(item_id: str, seconds: int) -> None:
    """Change how long one item stays on screen, in place (no re-add needed)."""
    items = _load()
    for it in items:
        if it["id"] == item_id:
            it["seconds"] = max(MIN_SECONDS, int(seconds))
            break
    _save(items)


def asset_path(ref: str) -> Path:
    return ASSETS / ref
