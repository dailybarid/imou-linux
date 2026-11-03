#!/usr/bin/env bash
# install.sh - set up Imou on Linux for the current user.
#
#   ./install.sh              install app + icon + menu entry
#   ./install.sh --service    also install and enable the recorder service
#
# Nothing here needs root: everything lands in ~/.local and ~/.config.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
VENV="$HERE/.venv"
ENVFILE="$HOME/.config/imou-camera.env"
SYS_PY="/usr/bin/python3"
WITH_SERVICE=0
[[ "${1:-}" == "--service" ]] && WITH_SERVICE=1

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

say "checking dependencies"
command -v ffmpeg >/dev/null 2>&1 || warn "ffmpeg not found on PATH — apt install ffmpeg"
"$SYS_PY" -c 'import tkinter' 2>/dev/null || {
    warn "python3-tk is missing — sudo apt install python3-tk"; exit 1; }
"$SYS_PY" -c 'import numpy' 2>/dev/null || warn "python3-numpy missing — apt install python3-numpy"

say "creating virtualenv"
# The distro python3-pil ships Pillow WITHOUT ImageTk (it lives in the separate
# python3-pil.imagetk package), so a venv with a full Pillow is required.
# --ignore-installed matters: with --system-site-packages pip otherwise sees the
# system Pillow as already satisfying the requirement and installs nothing.
"$SYS_PY" -m venv --system-site-packages "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet --ignore-installed pillow

if [[ ! -f "$ENVFILE" ]]; then
    say "camera credentials (stored in $ENVFILE, mode 600)"
    read -rp "  camera IP [192.168.1.100]: " CHOST; CHOST="${CHOST:-192.168.1.100}"
    read -rp "  username    [admin]: " CUSER; CUSER="${CUSER:-admin}"
    read -rsp "  password (device password / safety code): " CPASS; echo
    mkdir -p "$(dirname "$ENVFILE")"
    umask 077
    cat >"$ENVFILE" <<EOF
IMOU_HOST=$CHOST
IMOU_USER=$CUSER
IMOU_PASS=$CPASS
IMOU_SUBTYPE=0
IMOU_OUTDIR=$HOME/imou-recordings
IMOU_SEGMENT=300
IMOU_RETAIN_DAYS=30
EOF
    chmod 600 "$ENVFILE"
else
    say "keeping existing $ENVFILE"
fi

say "generating icon"
"$VENV/bin/python" "$HERE/make_icon.py" >/dev/null

say "installing menu entry"
mkdir -p "$HOME/.local/share/applications"
cat >"$HOME/.local/share/applications/imou-camera.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Imou Camera Control
GenericName=IP Camera
Comment=Live view, settings and SD card for a local Imou camera
Exec=$HERE/launch.sh
Icon=imou-camera
Terminal=false
Categories=AudioVideo;
Keywords=camera;cctv;surveillance;imou;rtsp;onvif;
StartupNotify=true
StartupWMClass=Imoucamera
EOF

if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

if [[ $WITH_SERVICE -eq 1 ]]; then
    say "installing recorder service"
    mkdir -p "$HOME/.config/systemd/user"
    sed "s|%h/Projects/imou-linux/imou-record.sh|$HERE/imou-record.sh|" \
        "$HERE/systemd/imou-recorder.service" \
        >"$HOME/.config/systemd/user/imou-recorder.service"
    systemctl --user daemon-reload
    systemctl --user enable --now imou-recorder.service
    loginctl enable-linger "$USER" 2>/dev/null || warn "could not enable linger (recording stops at logout)"
    systemctl --user --no-pager status imou-recorder.service || true
fi

say "done"
echo "   launch from the menu, or: $HERE/launch.sh"
