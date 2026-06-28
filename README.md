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

On the device (Raspberry Pi OS Lite 64-bit, or Debian 13 — no desktop needed).

A fresh Raspberry Pi OS Lite / minimal Debian image **does not include `git`**,
so pick one of the two ways below to get the code onto the device.

### Which role? Display vs Controller

You'll pick a role when you install. Every device runs the **same software** — the
role is just a starting choice, and you can switch it later (switching to
controller may pull in a few extra packages it needs).

- **Display** — a screen that simply shows whatever content (slides, images, web
  pages) gets sent to it. This is what most of your devices will be: it's
  lightweight and runs happily on the smallest hardware. When you're ready to
  connect it, it shows a short **pairing code** on the screen that you type into
  the controller.
- **Controller** — the one device you run everything from. Open its control panel
  from a phone or computer on the same network. It shows content itself too, but
  it also does the heavy lifting: turning PowerPoint files into images and pushing
  content out to all your displays. Pick controller for **exactly one** device on
  your network.

If you're not sure, choose **display** — and make whichever device you'll manage
things from the **controller**.

### Option A — one line, no git needed (recommended)

Download just the installer and let it do the rest. It installs everything it
needs (git included) and fetches the project itself. Choose the role with
`--role display` or `--role controller`:

    curl -sSL https://raw.githubusercontent.com/consecratedtech/signage/main/install.sh | sudo bash -s -- --role display

### Option B — install git, then clone

    sudo apt update && sudo apt install -y git
    git clone https://github.com/consecratedtech/signage.git
    cd signage
    sudo ./install.sh

Run from the cloned folder, the installer asks display/controller
interactively. If you ever see `Permission denied` on the script — copying from
Windows or unzipping a download can drop the executable bit — run it through
bash instead: `sudo bash install.sh`.

### What happens next

The installer checks the system, installs what it needs, and sets up the
boot-to-screen kiosk: the device powers on straight into full-screen content,
with no login prompt and no further manual steps. A reboot (or a power cut)
comes back up the same way on its own. Re-check the system any time with
`sudo ./install.sh --check`.

Then open the controller from your phone or computer at `http://<device-ip>:8080`.

> **Trying it on your computer first?** `run.sh` starts the app locally for a
> quick look — but don't use it on an installed device: the installed service
> already owns port 8080, so `run.sh` there will fail with "address already in
> use." That's expected, not a bug.

## Locking the control panel (optional)

By default the control panel is open to anyone on your local network. That keeps
a screen from ever locking you out, and is fine on a trusted office network. If
you want a gate, open **Settings → Password** on the controller and set a
username and password. After that, changing content or settings requires signing
in — but your displays keep playing either way, and the device-to-device pairing
and push stay cryptographically signed as before.

Forgot the password? On the device itself (a keyboard, or over SSH), run this
once to remove it and reopen the panel:

    sudo -u signage /opt/signage/.venv/bin/python -m app reset-password

## Goals

Built to one standard: it should fully work, and be nearly impossible to break.
See [LAWS.md](./LAWS.md).

## License

GPL-3.0-or-later. Copyright (C) 2026 Consecrated Tech.
