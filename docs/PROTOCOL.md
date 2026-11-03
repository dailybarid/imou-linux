# The method — how an Imou / Lechange camera works on a LAN

This is the protocol knowledge the project is built on, written up so it can be
reproduced on other Imou, Lechange, Dahua, Amcrest or OEM cameras that share the
same firmware family.

Nothing here is a vulnerability or an exploit. Every surface documented below is
a documented or de-facto standard interface that the camera serves to anyone who
authenticates with its own credentials.

## The governing insight

The vendor app is **not a LAN client**. It logs into the vendor cloud and tunnels
P2P to the device. That is the entire reason "it only works on Windows": there is
no Linux client because the client is not talking to the camera locally at all.

Meanwhile the camera itself speaks ONVIF, RTSP and (on Dahua-derived firmware)
DVRIP. Linux handles all three natively. So the camera is almost never the
problem — the missing piece is knowing which local interface has which capability.

The mirror image matters too: consumer firmware exposes ONVIF + RTSP + snapshot and
little else. There is no web UI and no CGI to "hack". Do not go looking for a back
door that was never shipped.

## What the camera serves

| Surface | Port | What it actually gives you |
|---|---|---|
| ONVIF | 80 | device info, media profiles, imaging, events |
| Snapshot | 80 | JPEG stills over HTTP digest |
| RTSP | 554 | live H.265/H.264, main + sub stream |
| DVRIP | 37777 | **config read/write and SD-card inventory** |
| DHHTTP / XAV | 8086 | second live stream (SDP + DHAV frames), live only |

Port **8086** is easy to miss. Omitting it from a scan leads to the wrong
conclusion that the device offers nothing but ONVIF.

## Discovery

Cameras drop ICMP, so a ping sweep finds nothing. **ARP is the reliable host
discovery**: a device answers ARP regardless of its firewall and then refuses
every probe.

```bash
ip neigh flush all
for i in $(seq 1 254); do ping -c1 -W1 192.168.1.$i &>/dev/null & done; wait
ip neigh | grep -v FAILED
```

If a scan misses a camera the owner swears is online, check the camera is powered
before you go re-architecting the network — an off camera is indistinguishable
from a VLAN isolation fault.

A camera can also answer on **two addresses** (dual DHCP lease, Wi-Fi plus
Ethernet, a repeater). Compare ONVIF serials before treating them as two devices.

## Authentication

Use the camera's own account — the device password / "safety code" set in the
vendor app, not the cloud account. Different stacks accept different schemes:

- ONVIF and the snapshot endpoint use **HTTP digest**
- RTSP uses **basic/digest** in the URL
- DVRIP uses its own login with MD5-derived hashes

Test **one credential attempt at a time**. These devices lock accounts after
roughly six bad attempts, for minutes at a time. A 401 on one stack does not mean
the password is wrong — it can mean the request shape is wrong.

## Live video over RTSP

The plain path returns **403 on a perfectly valid password**. The ONVIF flag is
mandatory:

```
rtsp://USER:PASS@HOST:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif
                                                                           ^^^^^^^^^^^
```

- `subtype=0` — main stream (e.g. 1920x1080 HEVC)
- `subtype=1` — sub stream (e.g. 640x480 H.264, for weak Wi-Fi or many cameras)

Without `&proto=Onvif` you get 403 and will wrongly conclude the credential is bad.

## Snapshots

```bash
curl --digest -u USER:PASS -o snap.jpg \
  "http://HOST/onvifsnapshot/media_service/snapshot?channel=1&subtype=0"
```

Cheapest and most legible proof that everything works.

## What the firmware does *not* implement

Everything below answers, but with a fault or an empty result. These are firmware
facts, not configuration mistakes — do not retry them with different parameters:

- ONVIF **Profile G / replay** → `ter:NotImplemented`
- ONVIF **PTZ** service is advertised on fixed-lens models that have no motors
- RTSP `/cam/playback` → `404` for every timestamp, subtype and proto variant
- DVRIP `RPC_Loadfile` → authenticates, then answers `500`
- Every XAV path on `:8086` except `/live/realmonitor.xav` → `500` (no handler)

The practical consequence: **clip bytes on the SD card are not retrievable over
the LAN** on this firmware class. Playback in the vendor app runs through their
cloud relay. The metadata, however, is fully readable — see below.

## DVRIP (TCP 37777) — the part that makes it useful

This is the surface that turns "no playback" into an actual storage inventory and
lets you change camera settings. It is a binary protocol: message header, then a
JSON body.

Login, then request/response pairs. Useful calls:

| Method | Gives you |
|---|---|
| `magicBox.getDeviceType` | model string |
| `storage.getDeviceAllInfo` | card path, total/used bytes, mount state |
| `mediaFileFind.factory.create` | a search token |
| `mediaFileFind.findFile` / `findNextFile` | every clip with path, size, duration, flags |
| `configManager.getConfig` | `VideoColor`, `VideoInOptions`, `MotionDetect`, `Record`, … |
| `configManager.setConfig` | writes the same table back |

Two details that cost hours if you miss them:

1. **`mediaFileFind.factory.create` returns the token in `result`, not `params`.**
   Reading the wrong field yields "no such token" and looks like a permissions bug.
2. **`setConfig` takes the whole table.** Read it, mutate the field you care about,
   write the entire table back. It is a round-trip, not a patch.

Clip paths look like:

```
/mnt/sd/2026-01-01/001/dav/16/16.15.00-16.20.00[R][0@0][0].dav
```

### The `Record` schedule bitmask

`configManager.getConfig name=Record` returns a table whose `TimeSection` is 8 rows
(7 days + holiday) of 6 slots. Each slot is `"<mask> HH:MM:SS-HH:MM:SS"`.

- `393222 00:00:00-23:59:59` — recording scheduled all day
- `0 00:00:00-23:59:59` — no recording

To turn SD recording on or off, rewrite the masks and write the table back. That
is exactly what the app's *Record to the camera's own SD card* toggle does.

## Recording locally instead

Since the card's bytes are unavailable over the network, record the RTSP stream to
a machine you control. This is arguably the better outcome: plain files, your
filesystem, no cloud relay.

```bash
ffmpeg -rtsp_transport tcp -timeout 15000000 -i "$RTSP_URL" \
  -c copy -f segment -segment_time 300 -segment_format mp4 \
  -reset_timestamps 1 -strftime 1 -movflags +faststart \
  "$OUTDIR/%Y-%m-%d_%H-%M-%S.mp4"
```

`-c copy` avoids re-encoding entirely — the CPU cost is near zero and quality is
untouched. Run it as a systemd **user** service with `loginctl enable-linger` so it
survives logout.

## If you pull the microSD

The `.dav` files are not a dead end. ffmpeg carries a native `dhav` demuxer:

```bash
ffmpeg -f dhav -i clip.dav -c copy clip.mp4
```

Verify with `ffmpeg -formats | grep dhav` — if your build lacks it, use a newer
ffmpeg rather than telling the user their footage is locked inside vendor tooling.

## Credits

DVRIP framing and the `imou-life` endpoint layout were informed by the community
work in [`home-assistant-plugins/imou-life`](https://github.com/home-assistant-plugins/imou-life).
The implementation here is independent Python.
