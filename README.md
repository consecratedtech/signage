# signage *(placeholder name)*

*Copyright (C) 2026 Consecrated Tech — created and maintained by Casey. Licensed under GPL-3.0-or-later.*

An **offline, appliance-style digital signage** system for small organizations —
churches, nonprofits, schools, clinics. Flash a Raspberry Pi (or repurpose an
old PC), and it boots straight into a screen you manage from your phone. No cloud
account, no subscription, and built so it **can't get wedged** by a power cut or
a bad update.

Every device runs the same software. One device wears the "controller" hat and
pushes content — a web page, images, Google Slides, or a PowerPoint converted to
slides — to the others over your local network. The role can move to any device
at any time.

> Read [`LAWS.md`](./LAWS.md) for the non-negotiable principles that guide every
> decision here. The short version: **fully works or we don't ship it**, and
> **it cannot break**.

---

## Try it locally (1 command)

No device needed — see the setup screen on your own computer:

```bash
./run.sh
```

Then open **http://localhost:8080**. (Or just double-click `app/pages/setup.html`
to preview the first-run screen with no install at all.)

## Install on a device

**What it runs on (all Debian 13 "Trixie"):**

| Hardware | OS to flash |
|---|---|
| Raspberry Pi 4 / 5 | Raspberry Pi OS **Lite (64-bit)** — via Raspberry Pi Imager |
| Old PC / ZimaBoard (64-bit) | **Debian 13** minimal (no desktop) |

Then, on the device:

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
sudo ./install.sh
```

The installer checks the OS and hardware, asks whether this device is a
**display** or the **controller**, installs only what that role needs, and sets
it up to boot straight into signage. Re-run diagnostics anytime with
`sudo ./install.sh --check`.

---

## Status

Early build. Working so far: the installer, first-boot identity + role,
the encrypted secret store, command signing, hostname sync, and the setup UI.

Roadmap: pairing handshake → LAN discovery → content pipeline → the full
control-panel UI → staged-swap-with-rollback updates, growing toward an
immutable A/B image.

## License

**GPL-3.0-or-later.** This keeps the project open: anyone can use and modify it,
but distributed modifications must stay open-source too — so no one can absorb
it into a closed product and strand the community.

Before publishing, drop in the full official license text (GitHub then detects it):

```bash
curl -fsSL https://www.gnu.org/licenses/gpl-3.0.txt -o LICENSE
```

Set the copyright holder in `LICENSE` — held by **Consecrated Tech** (author: Casey).

**Contributors:** if you want to keep the option to sell a commercial/closed
license later, require a **CLA** (Contributor License Agreement) before merging
outside code — otherwise contributors retain rights that block relicensing.
