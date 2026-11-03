#!/usr/bin/env bash
# Launch the Imou Camera Control GUI.
#
# Uses the project venv: the distro python3 ships Pillow WITHOUT ImageTk
# (it lives in the separate python3-pil.imagetk package), so system python
# cannot import ImageTk and the GUI dies instantly. .venv carries a complete
# Pillow. Errors are logged and surfaced, because a .desktop launcher has no
# terminal to print them to.
set -u

DIR="$(dirname "$(readlink -f "$0")")"
cd "$DIR" || exit 1

ENV_FILE="$HOME/.config/imou-camera.env"
if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY=/usr/bin/python3

LOG="${XDG_CACHE_HOME:-$HOME/.cache}/imou-camera.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

"$PY" ./imou_gui.py >>"$LOG" 2>&1
rc=$?

# 130/137/143 = SIGINT/SIGKILL/SIGTERM: an ordinary kill, logout or shutdown.
# Reporting those as a startup failure would pop a bogus dialog every shutdown.
case "$rc" in
    0 | 130 | 137 | 143) exit "$rc" ;;
esac

if [ "$rc" -ne 0 ]; then
    MSG="Imou Camera failed to start (exit $rc).

$(tail -n 12 "$LOG" 2>/dev/null)"
    for dlg in zenity kdialog xmessage; do
        if command -v "$dlg" >/dev/null 2>&1; then
            case "$dlg" in
                zenity) zenity --error --title="Imou Camera" --text="$MSG" 2>/dev/null ;;
                kdialog) kdialog --error "$MSG" 2>/dev/null ;;
                xmessage) xmessage -center "$MSG" 2>/dev/null ;;
            esac
            break
        fi
    done
fi
exit "$rc"
