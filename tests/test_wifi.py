# SPDX-License-Identifier: GPL-3.0-or-later
"""app/wifi.py is the app's side of the privileged network helper: it writes JSON
requests for the helper and reads back the results it writes. No nmcli or real
network is involved here — that all lives in the root helper, tested on hardware."""

import json

from app import wifi


def test_request_scan_writes_action(clean_data_dir):
    wifi.request_scan()
    assert wifi.pending() is True
    assert json.loads(wifi.REQUEST_PATH.read_text()) == {"action": "scan"}


def test_request_status_writes_action(clean_data_dir):
    wifi.request_status()
    assert json.loads(wifi.REQUEST_PATH.read_text()) == {"action": "status"}


def test_pending_false_with_no_request(clean_data_dir):
    assert wifi.pending() is False


def test_scan_empty_until_helper_writes(clean_data_dir):
    assert wifi.scan() == []


def test_scan_reads_helper_result(clean_data_dir):
    wifi.SCAN_PATH.write_text(json.dumps(
        {"networks": [{"ssid": "Home", "signal": 80, "secure": True}], "when": "x"}))
    assert wifi.scan() == [{"ssid": "Home", "signal": 80, "secure": True}]


def test_scan_survives_corrupt_file(clean_data_dir):
    wifi.SCAN_PATH.write_text("{not json")
    assert wifi.scan() == []


def test_status_empty_until_helper_writes(clean_data_dir):
    assert wifi.status() == {}


def test_status_reads_helper_result(clean_data_dir):
    wifi.STATUS_PATH.write_text(json.dumps(
        {"wifi": "connected", "ssid": "Home", "ap_active": False}))
    assert wifi.status()["ssid"] == "Home"
