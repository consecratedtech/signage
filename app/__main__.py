# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""Entry point: `python -m app` (this is what the systemd unit runs).

Ensures the data dir and Device ID exist, then starts the web service bound to
the LAN so the controller panel / display page is reachable from a phone.
"""

import sys

import uvicorn

from . import config, identity
from .server import create_app


def _reset_password() -> None:
    """Recovery: remove the control-panel password so the panel is open again.
    Run on the device console when the password is forgotten:
        sudo -u signage /opt/signage/.venv/bin/python -m app reset-password
    """
    from . import auth
    config.DATA.mkdir(parents=True, exist_ok=True)
    had = auth.has_credentials()
    auth.clear_credentials()
    print(f"Data dir: {config.DATA}")
    if had:
        print("Control-panel password removed. The panel is open again on this device.")
    else:
        print("No control-panel password was set here; nothing to remove.")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "reset-password":
        _reset_password()
        return
    config.DATA.mkdir(parents=True, exist_ok=True)
    config.WORK.mkdir(parents=True, exist_ok=True)
    identity.get_or_create_device_id()
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
