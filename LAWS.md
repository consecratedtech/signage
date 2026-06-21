# Project Laws

These are the non-negotiables. Every design and code decision is checked
against them. If a feature violates a law, the feature changes — not the law.

---

### 1. Fully works, or we don't ship it
A feature is either reliable enough to trust unattended on a wall for months,
or it doesn't go in. No "works most of the time." This is why video and
YouTube URLs are deferred until they're genuinely solid, and why PowerPoint is
converted to slides rather than rendered live.

### 2. It cannot break
The device must survive power cuts, network drops, and bad updates without ever
landing in a wedged or corrupted state. The worst-case outcome is "still
running the last good version," never "bricked."
- **Goal state:** an immutable (read-only) system with A/B updates — two system
  copies; updates are written to the spare slot and switched to only after
  they're verified, with automatic rollback to the last known-good version.
- **Ship-first step:** staged-swap-with-rollback for the app, growing into the
  full A/B immutable image.
- **App and OS update separately:** the app updates as a self-contained,
  rollback-able unit; the OS stays on Debian's stable security track. They never
  entangle. (This entanglement is exactly what wedges Anthias.)

### 3. Dumb light displays, smart central rendering
Displays only ever receive and show finished content (images, cached frames,
simple pages). All heavy work — PowerPoint conversion, page rendering — happens
on the controller. This keeps displays cheap, low-power, and able to run on the
smallest hardware. Every feature is checked: "does this keep the display
featherweight?"

### 4. Equal devices, one removable hat
Every device runs the same single image. "Controller" is a role toggled on one
device at a time, not a hardware tier. The role can move to any device at any
time (a confirmed hand-off), so there is no special box whose failure strands
the network.

### 5. Trust is anchored to the Device ID
The permanent, immutable Device ID is the root of trust. Aliases, hostnames, and
IP addresses can all change freely without breaking a paired link, because
pairing and command-signing key off the Device ID — never the name.

### 6. Never plaintext, ever
No secret is ever stored or transmitted in plaintext — not the admin password,
not keys, not pairing material. Passwords are Argon2-hashed; secrets are
AES-256-GCM encrypted at rest; commands are signed and verified.

### 7. A volunteer never needs a command line
If recovering from a normal problem requires the terminal, that's a bug in our
UX, not the user's fault. Setup, pairing, content, updates, and recovery are all
doable by an office secretary or volunteer from a simple screen.

### 8. Transparent by default
The user can always see what's happening, in plain language. Human-readable
activity in the UI ("Pushed slideshow to 3 screens," "Gym went offline 2:14pm"),
full technical logs in the journal for anyone who wants them, and a single live
health screen that reports problems with plain-English fixes.
