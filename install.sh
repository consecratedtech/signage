#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Consecrated Tech
#
# install.sh — bootstrap installer for the signage appliance (v1)
# Target: Debian 13 (trixie) on x86_64, and Raspberry Pi OS Lite (trixie) on Pi 4/5.
#
# Usage:
#   git clone <repo> && cd <repo> && sudo ./install.sh
#   ...or pipe it:  curl -sSL <raw-url>/install.sh | sudo bash
#
# Optional flags:
#   --role controller|display   Skip the interactive role prompt.
#   --force                     Continue even if the OS/arch check fails.
#   --check                     Run diagnostics only, install nothing.
#
set -euo pipefail

# ---- settings ---------------------------------------------------------------
APP="signage"                       # placeholder name — rename to your project
APP_USER="signage"
APP_HOME="/opt/${APP}"
DATA_DIR="/var/lib/${APP}"          # disk-backed; holds secrets + cached content
WORK_DIR="${DATA_DIR}/work"         # conversion scratch (NOT /tmp — tmpfs on trixie)
WEB_PORT="8080"
REPO_URL="${REPO_URL:-}"            # set when running via curl pipe
ROLE=""
FORCE=0
CHECK_ONLY=0

# ---- pretty output ----------------------------------------------------------
c() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok()   { echo "$(c '0;32' '  ok ') $*"; }
warn() { echo "$(c '0;33' 'warn ') $*"; }
die()  { echo "$(c '0;31' 'FAIL ') $*" >&2; exit 1; }
step() { echo; echo "$(c '1;36' "==> $*")"; }

# ---- args -------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ---- pre-flight checks ------------------------------------------------------
step "Pre-flight checks"

[ "$(id -u)" -eq 0 ] || die "run with sudo (need root to install packages)."

# OS: must be Debian 13 trixie (Raspberry Pi OS trixie reports ID=raspbian/debian, VERSION_CODENAME=trixie)
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
if [ "${VERSION_CODENAME:-}" = "trixie" ] || [ "${VERSION_ID:-}" = "13" ]; then
  ok "OS is Debian 13 / trixie (${PRETTY_NAME:-unknown})"
else
  warn "expected Debian 13 (trixie); found '${PRETTY_NAME:-unknown}'"
  [ "$FORCE" -eq 1 ] || die "unsupported OS. Re-run with --force to override."
fi

# Arch: arm64 (Pi 4/5) or amd64 (x86 PC)
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
  arm64|amd64) ok "architecture: ${ARCH}" ;;
  *) warn "untested architecture: ${ARCH}";
     [ "$FORCE" -eq 1 ] || die "unsupported arch. Re-run with --force to override." ;;
esac

# Disk space (need ~2 GB headroom; LibreOffice alone is large)
FREE_MB="$(df -Pm / | awk 'NR==2{print $4}')"
if [ "${FREE_MB:-0}" -lt 2048 ]; then
  warn "low free space on / : ${FREE_MB} MB (recommend >= 2048 MB)"
else
  ok "free space: ${FREE_MB} MB"
fi

# Network reachable for apt
if ping -c1 -W2 deb.debian.org >/dev/null 2>&1; then
  ok "network reachable"
else
  warn "could not reach deb.debian.org — apt may fail"
fi

# Which chromium package exists on this OS?
CHROMIUM_PKG=""
for p in chromium chromium-browser; do
  if apt-cache show "$p" >/dev/null 2>&1; then CHROMIUM_PKG="$p"; break; fi
done
[ -n "$CHROMIUM_PKG" ] && ok "chromium package: ${CHROMIUM_PKG}" || warn "no chromium package found in apt"

if [ "$CHECK_ONLY" -eq 1 ]; then
  step "Check-only mode: stopping before any changes."
  exit 0
fi

# ---- role -------------------------------------------------------------------
step "Role"
if [ -z "$ROLE" ]; then
  echo "Every device runs the same app. Pick this device's starting role:"
  echo "  1) display     — shows content (lightweight; runs on small devices)"
  echo "  2) controller  — also displays, plus runs the control panel + conversion"
  read -rp "Enter 1 or 2: " choice
  case "$choice" in
    1) ROLE="display" ;;
    2) ROLE="controller" ;;
    *) die "invalid choice." ;;
  esac
fi
case "$ROLE" in
  display|controller) ok "role: ${ROLE}" ;;
  *) die "role must be 'display' or 'controller'." ;;
esac
echo "(role is switchable later in settings; switching to controller will fetch the extra packages then.)"

# ---- packages ---------------------------------------------------------------
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# common to both roles
COMMON_PKGS="python3 python3-venv python3-dev python3-pip \
  git curl ca-certificates \
  avahi-daemon avahi-utils \
  cage ${CHROMIUM_PKG} \
  fonts-dejavu"

# controller also renders/converts, so it needs LibreOffice + PDF->image tools
CONTROLLER_PKGS="libreoffice-impress libreoffice-core poppler-utils"

PKGS="$COMMON_PKGS"
[ "$ROLE" = "controller" ] && PKGS="$PKGS $CONTROLLER_PKGS"

# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $PKGS
ok "system packages installed"

# ---- app user + dirs --------------------------------------------------------
step "Creating app user and directories"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/${APP_USER}" \
          --shell /usr/sbin/nologin "$APP_USER"
  ok "created user ${APP_USER}"
else
  ok "user ${APP_USER} already exists"
fi
# render group lets cage/chromium reach the GPU/DRM device on display nodes
usermod -aG video,render,input "$APP_USER" 2>/dev/null || true
# linger gives this (system) user a persistent /run/user/<uid>; cage needs it for
# XDG_RUNTIME_DIR even though the kiosk runs from systemd, not an interactive login.
loginctl enable-linger "$APP_USER" 2>/dev/null || true

install -d -m 0755 "$APP_HOME"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$WORK_DIR"
ok "data dir ${DATA_DIR} (0700 — secrets stay here, never world-readable)"

# ---- app code ---------------------------------------------------------------
step "Placing app code"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SRC_DIR}/requirements.txt" ]; then
  ok "running from a checked-out repo"
elif [ -n "$REPO_URL" ]; then
  ok "cloning ${REPO_URL}"
  rm -rf "${WORK_DIR}/_src"
  git clone --depth 1 "$REPO_URL" "${WORK_DIR}/_src"
  SRC_DIR="${WORK_DIR}/_src"
else
  die "no app source found. Run from the cloned repo, or set REPO_URL when piping."
fi
cp -a "${SRC_DIR}/." "${APP_HOME}/"
# Never ship a copied-in dev virtualenv or repo metadata: a stale .venv carries
# absolute-path shebangs from the source checkout (breaks pip), and .git is dead
# weight on the appliance. The venv below is always built fresh.
rm -rf "${APP_HOME}/.venv" "${APP_HOME}/.git"
chown -R "$APP_USER":"$APP_USER" "$APP_HOME"
ok "code copied to ${APP_HOME}"

# ---- python venv ------------------------------------------------------------
step "Setting up Python environment (venv — required on trixie/PEP 668)"
sudo -u "$APP_USER" python3 -m venv --clear "${APP_HOME}/.venv"
sudo -u "$APP_USER" "${APP_HOME}/.venv/bin/pip" install --quiet --upgrade pip
if [ -f "${APP_HOME}/requirements.txt" ]; then
  sudo -u "$APP_USER" "${APP_HOME}/.venv/bin/pip" install --quiet -r "${APP_HOME}/requirements.txt"
  ok "python dependencies installed in venv"
else
  warn "no requirements.txt — skipping pip install"
fi

# ---- systemd: the app service ----------------------------------------------
step "Installing services"
cat > "/etc/systemd/system/${APP}.service" <<EOF
[Unit]
Description=${APP} agent (${ROLE})
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
User=${APP_USER}
Environment=SIGNAGE_ROLE=${ROLE}
Environment=SIGNAGE_DATA=${DATA_DIR}
Environment=SIGNAGE_WORK=${WORK_DIR}
Environment=SIGNAGE_PORT=${WEB_PORT}
WorkingDirectory=${APP_HOME}
ExecStart=${APP_HOME}/.venv/bin/python -m app
Restart=always
RestartSec=3
# hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
ok "${APP}.service installed (auto-restart, sandboxed, can only write ${DATA_DIR})"

# kiosk: cage launches Chromium fullscreen pointing at the LOCAL screen page.
# the app serves http://localhost:PORT/screen which renders this device's cached playlist.
cat > "/etc/systemd/system/${APP}-kiosk.service" <<EOF
[Unit]
Description=${APP} kiosk display
After=${APP}.service systemd-user-sessions.service getty@tty1.service
Wants=${APP}.service
# Take the console VT away from the login prompt so the kiosk owns the screen.
Conflicts=getty@tty1.service

[Service]
User=${APP_USER}
# A real login session (PAM) is what gives cage a logind seat on seat0 — that
# seat is how wlroots becomes DRM master. Claiming tty1 as the controlling
# terminal is what makes the session "active" so logind hands over the seat.
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty-fail
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
UtmpMode=user
# cage (wlroots) needs XDG_RUNTIME_DIR; enable-linger (above) creates /run/user/<uid>.
Environment=XDG_RUNTIME_DIR=/run/user/%U
Environment=XDG_SESSION_TYPE=wayland
ExecStart=/usr/bin/cage -- ${CHROMIUM_PKG} \\
  --kiosk --noerrdialogs --disable-infobars --incognito \\
  --check-for-update-interval=31536000 \\
  http://localhost:${WEB_PORT}/screen
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
ok "${APP}-kiosk.service installed (cage + ${CHROMIUM_PKG}, boots straight to fullscreen)"

systemctl daemon-reload
systemctl enable --now "${APP}.service"      >/dev/null 2>&1 || warn "could not start ${APP}.service yet (app code may be incomplete)"
systemctl enable --now "${APP}-kiosk.service" >/dev/null 2>&1 || warn "could not start kiosk yet"

# ---- done -------------------------------------------------------------------
step "Done"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Role:        ${ROLE}"
echo "App:         ${APP_HOME}"
echo "Data/secrets:${DATA_DIR} (0700)"
if [ "$ROLE" = "controller" ]; then
  echo "Control panel: http://${IP:-<this-device-ip>}:${WEB_PORT}/"
else
  echo "This display:  http://${IP:-<this-device-ip>}:${WEB_PORT}/  (shows its pairing code when you start pairing)"
fi
echo
echo "Re-run diagnostics anytime:  sudo ./install.sh --check"
