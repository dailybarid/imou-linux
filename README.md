<p align="center">
  <img src="assets/banner.png" alt="Imou on Linux — local RTSP, ONVIF and DVRIP control" width="100%">
</p>

<p align="center">
  <em>Full local access to Imou / Lechange (Dahua-chipset) cameras from Linux —<br>
  live view, image &amp; night tuning, motion detection, SD-card inventory and local recording.</em>
</p>

<p align="center">
  <img alt="platform" src="https://img.shields.io/badge/platform-Linux-2f3542?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-2f3542?style=flat-square">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-2f3542?style=flat-square">
  <img alt="no cloud" src="https://img.shields.io/badge/cloud-none-4d8dff?style=flat-square">
</p>

---

## Why this exists

Imou ships no Linux client. The Windows and mobile apps are **not LAN clients** —
they authenticate against Imou's cloud and tunnel P2P to the device. That is the
whole reason the camera "only works on Windows".

The camera itself is not the problem. It speaks **RTSP, ONVIF and DVRIP**, and Linux
handles all three natively. This is the toolset that came out of mapping exactly
which local interfaces do what — and which ones the firmware quietly refuses.

The full protocol write-up is **[docs/PROTOCOL.md](docs/PROTOCOL.md)**. That is the
interesting part if you own a different Imou, Lechange, Dahua or Amcrest camera.

## What you get

| | |
|---|---|
| **Live view** | 1080p main or 640×480 sub stream, in-app |
| **Snapshots** | one click, JPEG straight from the camera |
| **Image tuning** | brightness, contrast, saturation, hue, gamma, chroma, style |
| **Night / optics** | auto / colour / B&W, IR LED, switch sensitivity, white balance, anti-flicker, mirror, flip |
| **Motion** | enable + sensitivity, read from and written to the device |
| **SD card** | capacity, plus every clip with times, duration, size and motion events |
| **Local recording** | 24/7 segmented MP4 to your disk, with retention, surviving logout |
| **Settings** | autostart, recorder on/off, segment length, retention, camera-SD toggle |

Everything runs on your LAN. No account, no cloud, no relay.

## Install

Requirements: Python 3.10+, `ffmpeg`, `python3-tk`, `python3-numpy`.

```bash
git clone https://github.com/dailybarid/imou-linux.git
cd imou-linux
./install.sh              # app + icon + menu entry
./install.sh --service    # ...and the 24/7 recorder as a systemd user service
```

The installer asks for the camera's own account — the **device password / safety
code** set in the vendor app, *not* your Imou cloud login — and writes it to
`~/.config/imou-camera.env` with mode `600`.

Then launch **Imou Camera Control** from your application menu.

> **Why a venv?** The distro `python3-pil` ships Pillow *without* `ImageTk` (it
> lives in the separate `python3-pil.imagetk` package), so system Python dies on
> `from PIL import ImageTk` — silently, when started from a menu. The installer
> builds a venv with a complete Pillow. Note `--ignore-installed`: with
> `--system-site-packages`, pip otherwise sees the system Pillow as already
> satisfying the requirement and installs nothing.

## Use it

```bash
./launch.sh                       # GUI
./imou_sd.py --storage            # SD card usage
./imou_sd.py --list 7             # every clip from the last 7 days
./imou_sd.py --list 7 --json      # same, machine-readable
./imou-record.sh --test 20        # record 20 s to prove the pipeline
```

The recorder writes `~/imou-recordings/YYYY-mm-dd_HH-MM-SS.mp4` — 1080p HEVC,
roughly 1.7 GB/day, pruned to your configured retention.

### Settings tab

These controls act on real system state, not local preferences:

| Control | What it actually does |
|---|---|
| Start Imou Camera when I log in | writes/removes `~/.config/autostart/imou-camera.desktop` |
| Record continuously | `systemctl --user enable --now` / `disable --now` |
| Segment / Keep for | writes `IMOU_SEGMENT` + `IMOU_RETAIN_DAYS`, restarts the recorder |
| Record to the camera's own SD card | rewrites the camera's `Record` schedule (bitmask `393222` on / `0` off) |
| View log | opens `~/.cache/imou-camera.log` |

Toggles read live state on startup, so they show what is true rather than what was
last clicked. If a camera read fails, the SD toggle stays at "checking…" instead of
falsely reporting "disabled".

## Configuration

`~/.config/imou-camera.env` — see
[imou-camera.env.example](imou-camera.env.example):

```ini
IMOU_HOST=192.168.1.100
IMOU_USER=admin
IMOU_PASS=your-device-password
IMOU_SUBTYPE=0          # 0 = main 1080p, 1 = sub
IMOU_OUTDIR=/home/you/imou-recordings
IMOU_SEGMENT=300
IMOU_RETAIN_DAYS=30
```

## The one thing to remember

RTSP returns **403 on a perfectly valid password** unless you append the ONVIF flag:

```
rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif
```

Without `&proto=Onvif` you will conclude the credential is wrong. It is not.

## Honest limitations

Firmware facts, not bugs to report:

- **SD playback over the network is impossible on this firmware class.** Metadata
  yes, clip bytes no: `/RPC_Loadfile` returns `500`, RTSP `/cam/playback` returns
  `404`, and `:8086` is live-only. Local recording is the answer.
- **PTZ appears in ONVIF** but fixed-lens models have no motors.
- **`ter:NotImplemented`** from ONVIF means the firmware simply does not do it.

If you pull the microSD, the `.dav` clips remux losslessly:
`ffmpeg -f dhav -i clip.dav -c copy clip.mp4`.

## Privacy

No camera screenshots, serial numbers or credentials are published in this
repository, and captured media, the credential file and local settings are all
gitignored. A screenshot of a working camera is a picture of someone's home or
street — it does not belong in a public repo.

The password sits in a `600` file because the systemd unit and the launcher must
read it non-interactively. If that is not acceptable, use `systemd-creds` or run
the GUI with `IMOU_PASS` exported in your own session.

## Credits

The DVRIP surface and the `:8086` XAV layout were informed by the community work in
[`home-assistant-plugins/imou-life`](https://github.com/home-assistant-plugins/imou-life).
This implementation is independent Python.

## License

[MIT](LICENSE)
