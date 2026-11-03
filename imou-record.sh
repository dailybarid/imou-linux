#!/usr/bin/env bash
# imou-record.sh - record an Imou/Dahua camera's RTSP stream to segmented MP4s.
#
# Usage:
#   IMOU_PASS=xxxx ./imou-record.sh                 # run in foreground
#   IMOU_PASS=xxxx ./imou-record.sh --test 20       # record 20s then exit
#
# Env: IMOU_HOST (192.168.1.100) IMOU_USER (admin) IMOU_PASS
#      IMOU_SUBTYPE 0=1080p main  1=640x480 sub
#      IMOU_OUTDIR  IMOU_SEGMENT (seconds per file) IMOU_RETAIN_DAYS
set -uo pipefail

HOST="${IMOU_HOST:-192.168.1.100}"
USER_="${IMOU_USER:-admin}"
PASS="${IMOU_PASS:?set IMOU_PASS}"
SUBTYPE="${IMOU_SUBTYPE:-0}"
OUTDIR="${IMOU_OUTDIR:-$HOME/imou-recordings}"
SEGMENT="${IMOU_SEGMENT:-300}"
RETAIN_DAYS="${IMOU_RETAIN_DAYS:-30}"

FFMPEG="${FFMPEG:-$(command -v ffmpeg || echo /usr/bin/ffmpeg)}"
URL="rtsp://${USER_}:${PASS}@${HOST}:554/cam/realmonitor?channel=1&subtype=${SUBTYPE}&unicast=true&proto=Onvif"

TEST=""
if [[ "${1:-}" == "--test" ]]; then TEST="${2:-15}"; fi

mkdir -p "$OUTDIR" || { echo "cannot create $OUTDIR" >&2; exit 1; }

# one recorder per host+subtype
LOCK="/tmp/imou-record-${HOST}-${SUBTYPE}.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "recorder already running for ${HOST} subtype ${SUBTYPE}" >&2
  exit 0
fi

prune() {
  find "$OUTDIR" -maxdepth 1 -type f -name '*.mp4' -mtime "+${RETAIN_DAYS}" -delete 2>/dev/null
}
trap 'exit 0' INT TERM

echo "$(date '+%F %T') recording ${HOST} subtype=${SUBTYPE} -> ${OUTDIR} (${SEGMENT}s segments, ${RETAIN_DAYS}d retention)"

ARGS=(
  -hide_banner -loglevel warning -nostdin
  -rtsp_transport tcp
  -timeout 15000000
  -i "$URL"
  -c copy
  -f segment
  -segment_time "$SEGMENT"
  -segment_format mp4
  -reset_timestamps 1
  -strftime 1
  -movflags +faststart
)
if [[ -n "$TEST" ]]; then
  "$FFMPEG" "${ARGS[@]}" -t "$TEST" "${OUTDIR}/%Y-%m-%d_%H-%M-%S.mp4"
  echo "test recording finished"; ls -lh "$OUTDIR" | tail -3; exit 0
fi

# long run: restart on failure, prune daily
while true; do
  "$FFMPEG" "${ARGS[@]}" "${OUTDIR}/%Y-%m-%d_%H-%M-%S.mp4"
  rc=$?
  prune
  echo "$(date '+%F %T') ffmpeg exited rc=$rc, restarting in 10s"
  sleep 10
done
