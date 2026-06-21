# signage *(placeholder name)*

Offline digital signage for small organizations — churches, nonprofits, schools,
and clinics. Flash a Raspberry Pi (or repurpose an old 64-bit PC) and it boots
straight into a screen you manage from your phone. No cloud account, no
subscription — and built so a power cut or a bad update can't wedge it.

One device runs the controls and pushes content — a web page, images, Google
Slides, or a PowerPoint turned into slides — to the others over your local
network. Every device runs the same software, and any one of them can be the
controller.

## What you need

- A **Raspberry Pi 4 or 5** running **Raspberry Pi OS Lite (64-bit)**, or
- A **64-bit PC** running **Debian 13** (no desktop needed).

## Get started

On the device:

    git clone https://github.com/consecratedtech/signage.git
    cd signage
    sudo ./install.sh

The installer checks the system, asks whether this device shows content (a
display) or runs the controls, installs what it needs, and sets it to start on
boot. Re-check anything later with `sudo ./install.sh --check`.

Then open the controller from your phone or computer at `http://<device-ip>:8080`.

## Goals

Built to one standard: it should fully work, and be nearly impossible to break.
See [LAWS.md](./LAWS.md).

## License

GPL-3.0-or-later. Copyright (C) 2026 Consecrated Tech.
