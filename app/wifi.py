# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""WiFi setup — talk to the privileged network helper.

The app is sandboxed and can't run nmcli itself, so to do anything with the network
it drops a small JSON request in the data dir. A root-owned path unit (installed by
install.sh) runs the helper, which performs the one action and writes the result
back to a file here. The app only ever writes a request and reads a result — it
holds no network privilege of its own.
"""

import json

from . import config

REQUEST_PATH = config.DATA / "wifi.request"
SCAN_PATH = config.DATA / "wifi-scan.json"
STATUS_PATH = config.DATA / "wifi-status.json"


def _request(payload: dict) -> None:
    """Drop a request for the helper. Written atomically so the watcher never sees
    a half-written file."""
    try:
        config.DATA.mkdir(parents=True, exist_ok=True)
        tmp = REQUEST_PATH.with_suffix(".request.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(REQUEST_PATH)
    except OSError:
        pass


def request_scan() -> None:
    """Ask the helper to list nearby networks; the result lands in wifi-scan.json."""
    _request({"action": "scan"})


def request_status() -> None:
    """Ask the helper to refresh what the radio is doing (wifi-status.json)."""
    _request({"action": "status"})


def scan() -> list:
    """Nearby networks the helper last found — [{ssid, signal, secure}], strongest
    first. Empty until a scan has run (or if the helper isn't installed)."""
    try:
        return json.loads(SCAN_PATH.read_text()).get("networks", [])
    except (json.JSONDecodeError, OSError):
        return []


def status() -> dict:
    """The helper's last network status: {wifi, ssid, ap_active, when}, or {}."""
    try:
        return json.loads(STATUS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def pending() -> bool:
    """True while a request is still waiting for the helper to pick it up."""
    return REQUEST_PATH.exists()
