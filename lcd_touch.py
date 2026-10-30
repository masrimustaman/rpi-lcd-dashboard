"""Touch + calibration support for the 3.5" LCD status panel (ADS7846)."""
import json
import os
import select
import struct
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

W, H = 480, 320
EVENT_FMT = "=qqHHi"
EVENT_SIZE = 24
CAL_DIR = "/etc/lcd-status"
CAL_FILE = CAL_DIR + "/touch.json"
CAL_TARGETS = [(46, 46), (434, 46), (434, 258), (46, 258)]

BG = (11, 15, 20)
TRACK = (34, 48, 60)
DIM = (124, 140, 158)
ACCENT = (61, 220, 151)


class Touch:
    def __init__(self):
        self.fd = None
        self.path = None
        self.last = None
        self.pressed = False
        self.press_t = 0.0
        self._find()

    def _find(self):
        name = None
        for i in range(32):
            try:
                with open(f"/sys/class/input/event{i}/device/name") as f:
                    name = f.read().strip()
            except OSError:
                continue
            if "ADS7846" in name or "Touchscreen" in name:
                self.path = f"/dev/input/event{i}"
                break
        if not self.path:
            print("touch: no ADS7846 device found", file=sys.stderr)
            return
        try:
            self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            print(f"touch: using {self.path}", file=sys.stderr)
        except OSError as exc:
            print(f"touch open failed: {exc}", file=sys.stderr)
            self.fd = None

    def events(self, timeout=0.1):
        if self.fd is None:
            time.sleep(timeout)
            return
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return
        try:
            buf = os.read(self.fd, EVENT_SIZE * 64)
        except OSError:
            return
        for off in range(0, len(buf) - EVENT_SIZE + 1, EVENT_SIZE):
            _, _, etype, code, value = struct.unpack(EVENT_FMT, buf[off:off + EVENT_SIZE])
            yield etype, code, value


class Calibration:
    def __init__(self, px=None, py=None):
        self.px = px
        self.py = py

    @property
    def ok(self):
        return self.px is not None and self.py is not None

    def map(self, raw):
        rx, ry = raw
        x = self.px[0] * rx + self.px[1] * ry + self.px[2]
        y = self.py[0] * rx + self.py[1] * ry + self.py[2]
        return (int(round(min(max(x, 0), W - 1))), int(round(min(max(y, 0), H - 1))))

    def save(self):
        os.makedirs(CAL_DIR, exist_ok=True)
        with open(CAL_FILE, "w") as f:
            json.dump({"px": list(self.px), "py": list(self.py)}, f)


def load_cal():
    try:
        with open(CAL_FILE) as f:
            d = json.load(f)
        c = Calibration(d["px"], d["py"])
        # reject a stored fit that would have been degenerate
        if max(abs(c.px[0]), abs(c.px[1]), abs(c.py[0]), abs(c.py[1])) > 1.0:
            print("cal: stored fit is degenerate, ignoring", file=sys.stderr)
            return Calibration()
        return c
    except (OSError, ValueError, KeyError):
        return Calibration()


def fit_calibration(raw_pts, scr_pts):
    """Least-squares affine fit, with conditioning checks the old build lacked."""
    a = np.array([[rx, ry, 1.0] for rx, ry in raw_pts], dtype=float)
    sx = np.array([p[0] for p in scr_pts], dtype=float)
    sy = np.array([p[1] for p in scr_pts], dtype=float)
    if np.ptp(a[:, 0]) < 300 or np.ptp(a[:, 1]) < 300:
        return None, "taps too clustered - spread to the corners"
    px = np.linalg.lstsq(a, sx, rcond=None)[0]
    py = np.linalg.lstsq(a, sy, rcond=None)[0]
    coef = max(abs(px[0]), abs(px[1]), abs(py[0]), abs(py[1]))
    if coef > 1.0:
        return None, f"ill-conditioned fit (coef {coef:.2f}) - retry, tap corners"
    resid = max(float(np.abs(a @ px - sx).max()), float(np.abs(a @ py - sy).max()))
    if resid > 25:
        return None, f"residual {resid:.0f}px too high - retry"
    return Calibration(px, py), f"ok resid={resid:.1f}px coef={coef:.4f}"


def font(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/" + name, size)
    except OSError:
        return ImageFont.load_default()


def render_calibration(idx, msg=""):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((W // 2, 12), "TOUCH CALIBRATION", font=font(18, True), fill=ACCENT, anchor="ma")
    d.text((W // 2, 38), f"tap the highlighted circle ({idx + 1} of 4)",
           font=font(12), fill=DIM, anchor="ma")
    for i, (tx, ty) in enumerate(CAL_TARGETS):
        active = i == idx
        r = 16 if active else 9
        d.ellipse((tx - r, ty - r, tx + r, ty + r), outline=ACCENT if active else TRACK, width=3)
        if active:
            d.ellipse((tx - 4, ty - 4, tx + 4, ty + 4), fill=ACCENT)
    if msg:
        d.text((W // 2, H - 40), msg, font=font(12), fill=(255, 180, 84), anchor="ma")
    d.text((W // 2, H - 20), "fingernail or stylus works best", font=font(11), fill=DIM, anchor="ma")
    return img
