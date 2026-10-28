#!/usr/bin/env python3
"""3.5" SPI LCD status panel for Raspberry Pi OS (fbtft /dev/fb0, RGB565).

Renders hostname, addresses, CPU, memory, disk and uptime straight into the
framebuffer every few seconds.  fbcon is detached while the service runs so the
kernel console cannot draw over the panel; it is re-attached on shutdown.

Read-only by design: this build has no touch interaction.
"""
import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont
import numpy as np

W, H = 480, 320
FB = "/dev/fb0"
INTERVAL = 3.0

BG = (11, 15, 20)
CARD = (21, 28, 36)
TRACK = (34, 48, 60)
TEXT = (230, 237, 243)
DIM = (124, 140, 158)
ACCENT = (61, 220, 151)
AMBER = (255, 180, 84)
RED = (255, 93, 93)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
_fcache = {}


def font(name, size):
    key = (name, size)
    if key not in _fcache:
        try:
            _fcache[key] = ImageFont.truetype(FONT_DIR + name, size)
        except OSError:
            _fcache[key] = ImageFont.load_default()
    return _fcache[key]


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def iface_ip(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


def primary_net():
    """First interface that actually holds an IPv4 address."""
    for name in ("eth0", "end0", "wlan0"):
        ip = iface_ip(name)
        if ip and not ip.startswith("127."):
            speed = read(f"/sys/class/net/{name}/speed", "")
            label = f"{speed} Mb/s" if speed.isdigit() and int(speed) > 0 else ""
            return name, ip, label
    return "-", "no address", ""


def meminfo():
    vals = {}
    for line in (read("/proc/meminfo", "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            vals[parts[0].rstrip(":")] = int(parts[1])
    total = vals.get("MemTotal", 0) // 1024
    avail = vals.get("MemAvailable", 0) // 1024
    return total, max(total - avail, 0), avail


def uptime_s():
    try:
        return float((read("/proc/uptime", "0") or "0").split()[0])
    except (ValueError, IndexError):
        return 0.0


def fmt_uptime(s):
    d, rem = divmod(int(s), 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def temp_c():
    raw = read("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.lstrip("-").isdigit():
        return int(raw) / 1000.0
    return 0.0


def cpu_ghz():
    for c in range(os.cpu_count() or 4):
        raw = read(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
        if raw and raw.isdigit():
            return int(raw) / 1e6
    return 0.0


def throttled():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        return out.split("=")[-1]
    except Exception:
        return "?"


class Wifi:
    """Read-only NetworkManager queries, cached so we don't spawn nmcli every frame."""

    def __init__(self):
        self._on = None
        self._on_ts = 0.0
        self._conn = None
        self._conn_ts = 0.0

    def radio_on(self, max_age=5.0):
        if self._on is None or time.time() - self._on_ts > max_age:
            try:
                out = subprocess.run(["nmcli", "-t", "-f", "WIFI", "radio"],
                                     capture_output=True, text=True,
                                     timeout=5).stdout.strip()
                self._on = out.lower().startswith("enabled")
            except Exception:
                self._on = False
            self._on_ts = time.time()
        return self._on

    def connection(self, max_age=15.0):
        if self._conn is None or time.time() - self._conn_ts > max_age:
            self._conn = ""
            try:
                out = subprocess.run(["nmcli", "-t", "-f", "GENERAL.CONNECTION",
                                      "device", "show", "wlan0"],
                                     capture_output=True, text=True, timeout=5).stdout
                for line in out.splitlines():
                    if line.startswith("GENERAL.CONNECTION:"):
                        self._conn = line.split(":", 1)[1].strip()
            except Exception:
                pass
            self._conn_ts = time.time()
        return self._conn


class Console:
    """Detach / re-attach fbcon around our exclusive use of the framebuffer."""

    def __init__(self):
        self.paths = []

    def detach(self):
        self.paths = [f"/sys/class/vtconsole/vtcon{i}/bind" for i in range(4)
                      if "frame buffer" in read(f"/sys/class/vtconsole/vtcon{i}/name", "")]
        for p in self.paths:
            try:
                with open(p, "w") as f:
                    f.write("0")
            except OSError:
                pass

    def attach(self):
        for p in self.paths:
            try:
                with open(p, "w") as f:
                    f.write("1")
            except OSError:
                pass


def render(wifi):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_host = font("DejaVuSans-Bold.ttf", 20)
    f_clock = font("DejaVuSans.ttf", 15)
    f_ip = font("DejaVuSans-Bold.ttf", 30)
    f_small = font("DejaVuSans.ttf", 12)
    f_tiny = font("DejaVuSans.ttf", 11)
    f_tag = font("DejaVuSans-Bold.ttf", 11)
    f_big = font("DejaVuSans-Bold.ttf", 25)

    d.text((14, 9), socket.gethostname(), font=f_host, fill=TEXT)
    d.text((W - 14, 14), time.strftime("%H:%M:%S"), font=f_clock, fill=DIM, anchor="ra")
    d.line((14, 40, W - 14, 40), fill=TRACK, width=1)

    iface, ip, speed = primary_net()
    d.text((14, 48), f"{iface}  {speed}".strip(), font=f_tag, fill=ACCENT)
    d.text((14, 62), ip, font=f_ip, fill=TEXT)
    d.text((W - 14, 54), "pilcd.local", font=f_small, fill=DIM, anchor="ra")

    wip = iface_ip("wlan0") if wifi.radio_on() else None
    if wip and iface != "wlan0":
        d.text((14, 98), f"wlan0 {wip}", font=f_tiny, fill=DIM)

    cw, ch = 223, 84
    xs = (12, 245)
    ys = (112, 204)

    def card(x, y, title):
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=8, fill=CARD)
        d.text((x + 13, y + 9), title, font=f_tag, fill=DIM)

    def bar(x, y, pct, color):
        w = cw - 26
        d.rounded_rectangle((x, y, x + w, y + 6), radius=3, fill=TRACK)
        fill_w = max(3, int(w * min(max(pct, 0), 100) / 100.0))
        d.rounded_rectangle((x, y, x + fill_w, y + 6), radius=3, fill=color)

    t = temp_c()
    tc = RED if t >= 75 else AMBER if t >= 65 else ACCENT
    card(xs[0], ys[0], "CPU")
    d.text((xs[0] + 13, ys[0] + 26), f"{t:.1f}\u00b0C", font=f_big, fill=tc)
    d.text((xs[0] + 13, ys[0] + 60), f"{cpu_ghz():.2f} GHz  \u00b7  load {os.getloadavg()[0]:.2f}",
           font=f_small, fill=DIM)

    total, used, avail = meminfo()
    pct = used * 100.0 / total if total else 0
    card(xs[1], ys[0], "MEMORY")
    d.text((xs[1] + 13, ys[0] + 26), f"{used} / {total} MB", font=f_big, fill=TEXT)
    bar(xs[1] + 13, ys[0] + 60, pct, ACCENT)
    d.text((xs[1] + 13, ys[0] + 69), f"{pct:.0f}% used  \u00b7  {avail} MB free",
           font=f_tiny, fill=DIM)

    st = os.statvfs("/")
    gib = 1024.0 ** 3
    tot_b = st.f_blocks * st.f_frsize
    used_b = tot_b - st.f_bfree * st.f_frsize
    free_b = st.f_bavail * st.f_frsize
    dpct = used_b * 100.0 / tot_b if tot_b else 0
    card(xs[0], ys[1], "DISK  /")
    d.text((xs[0] + 13, ys[1] + 26), f"{used_b / gib:.1f} / {tot_b / gib:.0f} GB",
           font=f_big, fill=TEXT)
    bar(xs[0] + 13, ys[1] + 60, dpct, AMBER if dpct > 80 else ACCENT)
    d.text((xs[0] + 13, ys[1] + 69), f"{dpct:.0f}% used  \u00b7  {free_b / gib:.0f} GB free",
           font=f_tiny, fill=DIM)

    thr = throttled()
    bad = thr not in ("0x0", "?")
    card(xs[1], ys[1], "UPTIME")
    d.text((xs[1] + 13, ys[1] + 26), fmt_uptime(uptime_s()), font=f_big, fill=TEXT)
    d.text((xs[1] + 13, ys[1] + 60), "throttle " + thr, font=f_small,
           fill=RED if bad else DIM)
    d.text((xs[1] + cw - 13, ys[1] + 60), "OK" if not bad else "WARN",
           font=f_tag, fill=RED if bad else ACCENT, anchor="ra")

    if not wifi.radio_on():
        left = "wlan0: radio off"
    elif wifi.connection():
        left = "wlan0: " + wifi.connection()
    else:
        left = "wlan0: not connected"
    d.text((14, 298), left[:38], font=f_tiny, fill=DIM)
    d.text((W - 14, 298), "ssh masuri@pilcd.local", font=f_tiny, fill=DIM, anchor="ra")
    return img


def to_rgb565(img):
    a = np.asarray(img, dtype=np.uint16)
    r = (a[:, :, 0] & 0xF8) << 8
    g = (a[:, :, 1] & 0xFC) << 3
    b = a[:, :, 2] >> 3
    return (r | g | b).astype("<u2").tobytes()


def main():
    con = Console()
    con.detach()
    wifi = Wifi()

    stop = {"flag": False}

    def bye(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    fb = None
    while not stop["flag"]:
        try:
            if fb is None:
                fb = open(FB, "r+b", buffering=0)
            fb.seek(0)
            fb.write(to_rgb565(render(wifi)))
        except FileNotFoundError:
            fb = None
            time.sleep(2)
            continue
        except Exception as exc:
            print(f"draw error: {exc}", file=sys.stderr)
            fb = None
            time.sleep(3)
            continue

        for _ in range(int(INTERVAL * 10)):
            if stop["flag"]:
                break
            time.sleep(0.1)

    if fb:
        fb.close()
    con.attach()


if __name__ == "__main__":
    main()
