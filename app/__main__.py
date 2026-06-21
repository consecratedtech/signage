# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
"""Entry point: `python -m app` (this is what the systemd unit runs).

Ensures the data dir and Device ID exist, then starts the web service bound to
the LAN so the controller panel / display page is reachable from a phone.
"""

import uvicorn

from . import config, identity
from .server import create_app


def main() -> None:
    config.DATA.mkdir(parents=True, exist_ok=True)
    config.WORK.mkdir(parents=True, exist_ok=True)
    identity.get_or_create_device_id()
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
