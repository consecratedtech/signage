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
# Where to fetch the code when this script is run on its own (the curl | bash
# one-liner) instead of from a checked-out repo. Override via env for a fork.
REPO_URL="${REPO_URL:-https://github.com/consecratedtech/signage.git}"
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

# ---- hide the mouse cursor --------------------------------------------------
# A signage screen has no operator, so the pointer must never show. cage ignores
# XCURSOR_THEME and loads the system "default" xcursor theme regardless, so we
# build a "blank" theme whose cursors are 1x1 fully-transparent images, alias
# every common cursor name to it, then repoint /usr/share/icons/default at it.
# (Pairing this with software cursors in the kiosk unit makes the blank theme
# actually take effect — see the WLR_NO_HARDWARE_CURSORS note below.)
step "Hiding the mouse cursor (blank xcursor theme)"
install -d -m 0755 /usr/share/icons/blank/cursors
python3 - <<'PY'
import struct
sizes=[16,24,32,48,64]; px=struct.pack('<I',0)
chunks=[struct.pack('<IIIIIIIII',36,0xfffd0002,s,1,1,1,0,0,0)+px for s in sizes]
hdr=b'Xcur'+struct.pack('<III',16,0x00010000,len(chunks))
pos=16+len(chunks)*12; offs=[]
for c in chunks: offs.append(pos); pos+=len(c)
toc=b''.join(struct.pack('<III',0xfffd0002,s,o) for s,o in zip(sizes,offs))
open('/usr/share/icons/blank/cursors/left_ptr','wb').write(hdr+toc+b''.join(chunks))
PY
( cd /usr/share/icons/blank/cursors
  for n in default pointer arrow top_left_arrow left_ptr_watch xterm text ibeam hand hand1 hand2 pointing_hand grab grabbing openhand closedhand watch wait progress crosshair cross fleur move all-scroll col-resize row-resize size_all size_ver size_hor n-resize e-resize s-resize w-resize ne-resize nw-resize se-resize sw-resize not-allowed no-drop forbidden help question_arrow context-menu copy alias; do
    ln -sf left_ptr "$n"
  done )
printf '[Icon Theme]\nName=blank\nComment=Invisible cursor for kiosk\n' > /usr/share/icons/blank/index.theme
install -d -m 0755 /usr/share/icons/default
ln -sfn /usr/share/icons/blank/cursors /usr/share/icons/default/cursors
ok "blank cursor theme installed and set as the system default"

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
# A private, writable /tmp for LibreOffice's IPC pipe during pptx conversion
# (ProtectSystem=strict makes the real /tmp read-only). The bulk conversion
# scratch still goes to the disk-backed work dir under ${DATA_DIR}.
PrivateTmp=true

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
# Hide the pointer: software cursors + the blank "default" theme (installed above)
# make it invisible. cage otherwise draws a GPU hardware cursor that ignores the
# theme, so WLR_NO_HARDWARE_CURSORS forces wlroots to render the (blank) cursor.
Environment=WLR_NO_HARDWARE_CURSORS=1
Environment=XCURSOR_THEME=blank
Environment=XCURSOR_PATH=/usr/share/icons
Environment=XCURSOR_SIZE=24
# Wait until the web app actually answers before launching the browser. systemd
# treats ${APP}.service as "started" the moment the process spawns, but uvicorn
# needs several seconds to import deps and bind the port; without this gate
# Chromium loads too early, gets ERR_CONNECTION_REFUSED, and never retries. On
# timeout this exits non-zero so Restart=always retries the whole unit.
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 60); do curl -sf http://localhost:${WEB_PORT}/healthz >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
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

# ---- promotion helper (display -> controller installs conversion packages) ---
# The app runs sandboxed (NoNewPrivileges) and cannot install packages itself.
# When a device is switched to the controller role it drops a request file in the
# data dir; this root-owned path unit notices it and installs the controller
# packages, writing progress back to a status file the UI reads. The package set
# is fixed here, so the app can only ever trigger this one specific install —
# never an arbitrary command.
step "Installing the controller-promotion helper"
cat > "/usr/local/sbin/${APP}-promote" <<EOF
#!/usr/bin/env bash
set -u
DATA_DIR="${DATA_DIR}"
STATUS="\${DATA_DIR}/promote.status"
REQ="\${DATA_DIR}/promote.request"
status(){ printf '{"state":"%s","detail":"%s","when":"%s"}\n' "\$1" "\$2" "\$(date -Is)" >"\$STATUS"; chown ${APP_USER}:${APP_USER} "\$STATUS" 2>/dev/null || true; }
status running "installing PowerPoint conversion packages"
export DEBIAN_FRONTEND=noninteractive
if apt-get update -qq && apt-get install -y --no-install-recommends ${CONTROLLER_PKGS}; then
  status done "PowerPoint conversion is ready"
else
  status failed "package install failed (the device needs internet to add PowerPoint support)"
fi
rm -f "\$REQ"
EOF
chmod 0755 "/usr/local/sbin/${APP}-promote"

cat > "/etc/systemd/system/${APP}-promote.service" <<EOF
[Unit]
Description=${APP} controller promotion (install PowerPoint conversion packages)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/${APP}-promote
EOF

cat > "/etc/systemd/system/${APP}-promote.path" <<EOF
[Unit]
Description=${APP} controller-promotion watcher

[Path]
PathExists=${DATA_DIR}/promote.request
Unit=${APP}-promote.service

[Install]
WantedBy=multi-user.target
EOF
ok "promotion helper installed (watches for a switch to the controller role)"

systemctl daemon-reload
systemctl enable "${APP}.service" "${APP}-kiosk.service" "${APP}-promote.path" >/dev/null 2>&1 || true
# Use restart (not just enable --now) so re-running the installer to UPDATE
# actually loads the new code — enable --now is a no-op on an already-running unit.
systemctl restart "${APP}.service"       >/dev/null 2>&1 || warn "could not start ${APP}.service yet (app code may be incomplete)"
systemctl restart "${APP}-kiosk.service" >/dev/null 2>&1 || warn "could not start kiosk yet"
systemctl restart "${APP}-promote.path"  >/dev/null 2>&1 || true

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
