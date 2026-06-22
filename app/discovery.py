# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""LAN discovery over mDNS (zeroconf).

Each device advertises itself so a controller can find displays without anyone
reading IP addresses off a screen. Manual add-by-address stays available as a
fallback for networks where mDNS is blocked.
"""

import socket
import time

from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

SERVICE_TYPE = "_signage._tcp.local."


def primary_ip() -> str:
    """Best-guess LAN address of this device."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def advertise(device_id: str, name: str, role: str, port: int) -> Zeroconf:
    """Publish this device on the network. Returns the Zeroconf handle, which
    the caller must keep alive for the advertisement to persist."""
    ip = primary_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{device_id}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"device_id": device_id, "name": name, "role": role},
        server=f"signage-{device_id[:8]}.local.",
    )
    zc = Zeroconf()
    zc.register_service(info)
    return zc


class _Collector:
    def __init__(self) -> None:
        self.found: dict = {}

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if not info:
            return
        props = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in (info.properties or {}).items()
        }
        address = socket.inet_ntoa(info.addresses[0]) if info.addresses else ""
        device_id = props.get("device_id", name)
        self.found[device_id] = {
            "device_id": props.get("device_id", ""),
            "name": props.get("name", ""),
            "role": props.get("role", ""),
            "address": address,
            "port": info.port,
        }

    def update_service(self, *args) -> None:
        pass

    def remove_service(self, *args) -> None:
        pass


def browse(timeout: float = 3.0) -> list:
    """Look for devices on the network for a few seconds; return what answered."""
    zc = Zeroconf()
    collector = _Collector()
    ServiceBrowser(zc, SERVICE_TYPE, collector)
    time.sleep(timeout)
    zc.close()
    return list(collector.found.values())
