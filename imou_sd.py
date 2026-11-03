#!/usr/bin/env python3
"""imou_sd.py - read an Imou/Lechange/Dahua camera's SD card from Linux.

The camera's HTTP/ONVIF/RTSP surfaces are live-only. The SD card IS readable
locally, but only over the Dahua DVRIP binary protocol on TCP/37777.

Usage:
    ./imou_sd.py --storage
    ./imou_sd.py --list 7
    ./imou_sd.py --list 7 --json > files.json

Credentials: env IMOU_USER / IMOU_PASS / IMOU_HOST, or flags.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import socket
import struct
import sys

PORT = 37777


# ---------- DVRIP transport ----------

def _gen1_hash(password: str) -> str:
    """Legacy Dahua 'compressor' hash."""
    s = hashlib.md5(password.encode("latin-1")).digest()
    out = [0] * 8
    i = j = 0
    while i < len(s):
        v = (s[i] + s[i + 1]) % 62
        v += 48 if v < 10 else (55 if v < 36 else 61)
        out[j] = v
        i += 2
        j += 1
    return "".join(chr(c) for c in out)


def _gen2(user, realm, rnd, pw):
    g = hashlib.md5(f"{user}:{realm}:{pw}".encode("latin-1")).hexdigest().upper()
    return hashlib.md5(f"{user}:{rnd}:{g}".encode("latin-1")).hexdigest().upper()


def _md5_login(user, rnd, pw):
    g1 = _gen1_hash(pw)
    return hashlib.md5(f"{user}:{rnd}:{g1}".encode("latin-1")).hexdigest().upper()


def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed")
        buf += chunk
    return buf


class Dvrip:
    def __init__(self, host, user, password, port=PORT, timeout=8):
        self.host, self.user, self.password, self.port = host, user, password, port
        self.timeout = timeout
        self.sock = None
        self.session = 0
        self.realm = None
        self._rid = 1

    def login(self):
        s = socket.create_connection((self.host, self.port), self.timeout)
        s.settimeout(self.timeout)
        self.sock = s
        s.sendall(struct.pack(">I", 0xA0010000) + b"\x00" * 20
                  + struct.pack(">Q", 0x050201010000A1AA))
        hdr = _recvn(s, 32)
        blen = struct.unpack("<I", hdr[4:8])[0]
        body = _recvn(s, blen).decode("latin-1", "replace") if blen else ""
        realm = re.search(r"Realm:([^\r\n]*)", body)
        rnd = re.search(r"Random:([^\r\n]*)", body)
        if not (realm and rnd and realm.group(1) and rnd.group(1)):
            raise RuntimeError("login challenge returned empty realm/random")
        self.realm = realm.group(1)
        r = rnd.group(1)
        h = self.user + "&&" + _gen2(self.user, self.realm, r, self.password) \
            + _md5_login(self.user, r, self.password)
        s.sendall(struct.pack(">I", 0xA0050000) + struct.pack("<I", len(h))
                  + b"\x00" * 16 + struct.pack(">Q", 0x050200080000A1AA)
                  + h.encode("latin-1"))
        resp = _recvn(s, 32)
        err = resp[8:10]
        self.session = struct.unpack("<I", resp[16:20])[0]
        extra = struct.unpack("<I", resp[4:8])[0]
        if extra:
            try:
                _recvn(s, extra)
            except Exception:
                pass
        if err != b"\x00\x08":
            codes = {b"\x01\x00": "auth failed", b"\x01\x01": "invalid user",
                     b"\x01\x04": "account locked (wait ~30 min)",
                     b"\x03\x03": "already logged in"}
            raise RuntimeError(f"login failed: {err.hex()} ({codes.get(err, 'unknown')})")
        return self.session

    def rpc(self, method, params=None):
        self._rid += 1
        obj = {"method": method, "id": self._rid, "session": self.session}
        if params is not None:
            obj["params"] = params
        data = json.dumps(obj).encode("latin-1")
        hdr = (struct.pack(">I", 0xF6000000) + struct.pack("<I", len(data))
               + struct.pack("<I", self._rid) + struct.pack("<I", 0)
               + struct.pack("<I", len(data)) + struct.pack("<I", 0)
               + struct.pack("<I", self.session) + struct.pack("<I", 0))
        self.sock.sendall(hdr + data)
        rh = _recvn(self.sock, 32)
        blen = struct.unpack("<I", rh[4:8])[0]
        body = _recvn(self.sock, blen) if blen else b""
        return json.loads(body.decode("latin-1", "replace")) if body else {}

    def close(self):
        if self.sock:
            self.sock.close()


# ---------- commands ----------

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def cmd_storage(c):
    r = c.rpc("storage.getDeviceAllInfo")
    info = (r.get("params") or {}).get("info") or []
    if not info:
        print("no storage reported:", json.dumps(r)[:300])
        return
    for dev in info:
        print(f"device : {dev.get('Name')}   state={dev.get('State')}")
        for d in dev.get("Detail") or []:
            total, used = d.get("TotalBytes", 0), d.get("UsedBytes", 0)
            pct = (used / total * 100) if total else 0
            print(f"  mount : {d.get('Path')} ({d.get('Type')})")
            print(f"  usage : {human(used)} / {human(total)}  ({pct:.0f}% used)")
            print(f"  free  : {human(total - used)}")
        for cid in dev.get("CIDInfo") or []:
            if cid:
                print(f"  card  : {cid.get('ProductName')} OEM={cid.get('OEMID')} "
                      f"sn={cid.get('ProductSerialNum')}")


def cmd_list(c, days, channel=0, limit=None):
    r = c.rpc("mediaFileFind.factory.create")
    tok = r.get("result")
    if not tok:
        print("could not create finder:", json.dumps(r)[:300], file=sys.stderr)
        return []
    end = datetime.datetime.now()
    start = end - datetime.timedelta(days=days)
    cond = {"Channel": channel,
            "StartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
            "Types": ["dav"], "Flags": ["Timing", "Event", "Manual"],
            "StreamType": 0}
    c.rpc("mediaFileFind.findFile", {"token": tok, "condition": cond})
    files = []
    while True:
        rn = c.rpc("mediaFileFind.findNextFile", {"token": tok, "count": 100})
        infos = (rn.get("params") or {}).get("infos") or []
        if not infos:
            break
        files.extend(infos)
        if len(infos) < 100:
            break
    try:
        c.rpc("mediaFileFind.close", {"token": tok})
        c.rpc("mediaFileFind.destroy", {"token": tok})
    except Exception:
        pass
    return files[:limit] if limit else files


def main():
    ap = argparse.ArgumentParser(description="Read an Imou/Dahua camera SD card from Linux")
    ap.add_argument("--host", default=os.environ.get("IMOU_HOST", "192.168.1.100"))
    ap.add_argument("--user", default=os.environ.get("IMOU_USER", "admin"))
    ap.add_argument("--pass", dest="password", default=os.environ.get("IMOU_PASS"))
    ap.add_argument("--storage", action="store_true", help="show SD card usage")
    ap.add_argument("--list", type=int, metavar="DAYS", help="list recordings from last N days")
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()
    if not a.password:
        ap.error("set IMOU_PASS or pass --pass")

    c = Dvrip(a.host, a.user, a.password)
    try:
        c.login()
    except Exception as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1

    try:
        if a.storage:
            if a.json:
                print(json.dumps(c.rpc("storage.getDeviceAllInfo"), indent=2))
            else:
                cmd_storage(c)
        if a.list is not None:
            files = cmd_list(c, a.list, a.channel, a.limit)
            if a.json:
                print(json.dumps(files, indent=2))
            else:
                total_bytes = sum(f.get("Length", 0) for f in files)
                print(f"{len(files)} recordings in last {a.list} day(s), "
                      f"{human(total_bytes)} total")
                print(f"{'start':<20}{'end':<10}{'dur':>6}{'size':>10}  flags")
                for f in files:
                    flags = ",".join(f.get("Flags") or [])
                    ev = ",".join(f.get("Events") or [])
                    print(f"{f.get('StartTime',''):<20}{f.get('EndTime',''):<10}"
                          f"{f.get('Duration',0):>5}s{human(f.get('Length',0)):>10}  "
                          f"{flags}{('/' + ev) if ev else ''}")
        if not a.storage and a.list is None:
            ap.print_help()
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
