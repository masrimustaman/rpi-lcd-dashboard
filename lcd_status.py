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

from lcd_touch import (Touch, Calibration, load_cal, fit_calibration,
                       render_calibration, CAL_TARGETS)

W, H = 480, 320
FB = "/dev/fb0"
INTERVAL = 3.0


def find_fb():
    """Locate the panel framebuffer by name/size (fb0/fb1 order changes on boot)."""
    for i in range(8):
        base = "/sys/class/graphics/fb%d" % i
        try:
            gname = open(base + "/name").read().strip().lower()
            vsize = open(base + "/virtual_size").read().strip()
        except OSError:
            continue
        if "ili9486" in gname or vsize.split(",")[0] == str(W):
            return "/dev/fb%d" % i
    return None

BTN_WIFI = (300, 46, 370, 82)
BTN_POWER = (378, 46, 448, 82)
MODAL_CANCEL = (60, 240, 220, 288)
MODAL_CONFIRM = (260, 240, 420, 288)


def inside(rect, x, y):
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

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
                # Resolve the active profile name, then ask that profile for its
                # SSID — the connection list does not expose the wireless field.
                out = subprocess.run(["nmcli", "-t", "-f", "GENERAL.CONNECTION",
                                      "device", "show", "wlan0"],
                                     capture_output=True, text=True, timeout=5).stdout
                profile = ""
                for line in out.splitlines():
                    if line.startswith("GENERAL.CONNECTION:"):
                        profile = line.split(":", 1)[1].strip()
                if profile:
                    out = subprocess.run(["nmcli", "-t", "-f", "802-11-wireless.ssid",
                                          "connection", "show", profile],
                                         capture_output=True, text=True, timeout=5).stdout
                    for line in out.splitlines():
                        if line.startswith("802-11-wireless.ssid:"):
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
    for rect, label in ((BTN_WIFI, "WiFi"), (BTN_POWER, "PWR")):
        d.rounded_rectangle(rect, radius=8, fill=CARD, outline=TRACK, width=1)
        d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), label,
               font=f_tag, fill=DIM, anchor="mm")

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


def render_modal(base, title, rows, hint, danger=False):
    img = base.copy()
    d = ImageDraw.Draw(img)
    d.rectangle((40, 58, W - 40, H - 30), fill=(15, 20, 26),
                outline=RED if danger else TRACK)
    d.text((60, 70), title, font=font("DejaVuSans-Bold.ttf", 16),
           fill=RED if danger else ACCENT)
    y = 100
    for k, v in rows:
        d.text((60, y), k, font=font("DejaVuSans.ttf", 12), fill=DIM)
        d.text((160, y), str(v)[:30], font=font("DejaVuSans.ttf", 13), fill=TEXT)
        y += 28
    d.text((W // 2, H - 46), hint, font=font("DejaVuSans.ttf", 11),
           fill=DIM, anchor="ma")
    return img


def render_confirm(base):
    img = render_modal(base, "SHUT DOWN pilcd?",
                       [("host", socket.gethostname()),
                        ("uptime", fmt_uptime(uptime_s()))],
                       "choose an action", danger=True)
    d = ImageDraw.Draw(img)
    for rect, label, col, bg in ((MODAL_CANCEL, "CANCEL", TEXT, CARD),
                                 (MODAL_CONFIRM, "SHUT DOWN", RED, (60, 20, 20))):
        d.rounded_rectangle(rect, radius=8, fill=bg,
                            outline=TRACK if bg == CARD else RED)
        d.text(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), label,
               font=font("DejaVuSans-Bold.ttf", 13), fill=col, anchor="mm")
    return img


def main():
    con = Console()
    con.detach()
    wifi = Wifi()
    touch = Touch()
    st = {"cal": load_cal(), "raw": [], "modal": None, "msg": ""}

    stop = {"flag": False}

    def bye(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    def screen_tap(x, y):
        if st["modal"] == "power":
            if inside(MODAL_CONFIRM, x, y):
                print("shutdown requested via panel", file=sys.stderr)
                subprocess.run(["systemctl", "poweroff"])
            st["modal"] = None
        elif st["modal"] == "wifi":
            st["modal"] = None
        elif inside(BTN_WIFI, x, y):
            st["modal"] = "wifi"
        elif inside(BTN_POWER, x, y):
            st["modal"] = "power"

    def handle_tap(raw):
        cal = st["cal"]
        if not cal.ok:
            st["raw"].append(raw)
            if len(st["raw"]) >= 4:
                c, note = fit_calibration(st["raw"][:4], CAL_TARGETS)
                st["raw"] = []
                st["msg"] = note
                if c:
                    c.save()
                    st["cal"] = c
                    print("calibration saved: " + note, file=sys.stderr)
                else:
                    print("calibration rejected: " + note, file=sys.stderr)
            return
        x, y = cal.map(raw)
        print(f"tap raw={raw} -> screen=({x},{y})", file=sys.stderr)
        screen_tap(x, y)

    def draw(fb):
        if not st["cal"].ok:
            img = render_calibration(len(st["raw"]), st["msg"])
        else:
            img = render(wifi)
            if st["modal"] == "wifi":
                rows = [("radio", "enabled" if wifi.radio_on() else "disabled"),
                        ("ssid", wifi.connection() or "not connected"),
                        ("wlan0", iface_ip("wlan0") or "-")]
                img = render_modal(img, "WIFI", rows, "tap anywhere to close")
            elif st["modal"] == "power":
                img = render_confirm(img)
        fb.seek(0)
        fb.write(to_rgb565(img))

    fb = None
    dirty = True
    last = 0.0
    while not stop["flag"]:
        for etype, code, value in touch.events(0.1):
            if etype == 3 and code == 0:
                touch.last = (value, touch.last[1] if touch.last else 0)
            elif etype == 3 and code == 1:
                touch.last = (touch.last[0] if touch.last else 0, value)
            elif etype == 1 and code == 330:
                if value == 1:
                    touch.pressed = True
                    touch.press_t = time.time()
                elif touch.pressed:
                    touch.pressed = False
                    if touch.last and time.time() - touch.press_t > 0.03:
                        handle_tap(touch.last)
                        dirty = True

        now = time.time()
        if not dirty and now - last < INTERVAL:
            continue
        try:
            if fb is None:
                path = find_fb() or FB
                fb = open(path, "r+b", buffering=0)
                print("fb: using " + path, file=sys.stderr)
            draw(fb)
            dirty = False
            last = now
        except FileNotFoundError as exc:
            print("fb missing: %s" % exc, file=sys.stderr)
            fb = None
            time.sleep(2)
        except Exception as exc:
            print(f"draw error: {exc}", file=sys.stderr)
            fb = None
            time.sleep(3)

    if fb:
        fb.close()
    con.attach()


if __name__ == "__main__":
    main()
