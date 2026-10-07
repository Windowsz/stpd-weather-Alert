"""Rain-radar nowcasting from the TMD Suvarnabhumi radar loop GIF.

The GIF (https://weather.tmd.go.th/svp/svpHQloop.gif) holds 12 frames, 15
minutes apart (timestamps are UTC), covering a 240 km radius around the radar.
Frames are plain images, so we:

1. classify echo pixels by matching them against the colour bar drawn on each
   frame (colour -> dBZ),
2. estimate how the echoes are moving (cross-correlation of the last two
   frames), and
3. look "upwind" of the target point to see whether rain is there now or is
   about to arrive within the next hour.

Image-to-map calibration (CENTER_PX, KM_PER_PX, RADAR_LAT/LON) was fitted by
eye against landmarks, so ETAs are approximate (a few km / minutes).
"""

import io
import logging
import math

import numpy as np
import requests
from PIL import Image

logger = logging.getLogger("rain-alert")

RADAR_GIF_URL = "https://weather.tmd.go.th/svp/svpHQloop.gif"
RADAR_DOWNLOAD_TIMEOUT = (10, 90)  # (connect, read) seconds; the GIF is ~14 MB

# Geometry of the 1920x1600 frames.
RADAR_LAT = 13.69
RADAR_LON = 100.76
CENTER_PX = (960, 800)  # (x, y) of the radar
KM_PER_PX = 0.3
RADAR_RANGE_KM = 235  # leave a margin inside the 240 km outer ring
FRAME_INTERVAL_MIN = 15

# Colour bar: 25 swatches, top (strongest) to bottom, drawn in the left strip.
BAR_X = 30
BAR_Y_START = 7
BAR_SWATCH_PX = 63.44
BAR_SWATCHES = 25
DBZ_TOP = 66.5  # lower edge of the top (white) swatch
DBZ_STEP = 2.478
COLOR_TOLERANCE = 6
MIN_ECHO_SPREAD = 100  # echo colours are vivid; map/terrain pixels are greyish
MIN_DBZ = 15  # ignore weaker returns (static ground clutter near the radar is below this)

# Rain definition and search geometry.
RADAR_DBZ_THRESHOLD = 25  # ~1.4 mm/h with Z = 200 R^1.6
TARGET_RADIUS_KM = 4  # echo within this distance of the point counts as "over it"
MOTION_WINDOW_KM = 70  # echoes this close to the point are used to estimate motion
MOTION_BLUR_PX = 6  # Gaussian sigma applied before correlating
MOTION_MIN_ECHO_PX = 150  # need this many echo pixels in both frames to trust motion
MOTION_MAX_KMH = 100
LEAD_MAX_MIN = 60  # how far ahead to extrapolate


def dbz_to_rain_rate(dbz):
    """Marshall-Palmer style Z-R conversion, mm/h."""
    return (10 ** (dbz / 10) / 200) ** (1 / 1.6)


def load_frames(gif_bytes):
    """Decode every frame of the GIF into RGB numpy arrays (oldest first)."""
    image = Image.open(io.BytesIO(gif_bytes))
    frames = []
    for index in range(image.n_frames):
        image.seek(index)
        frames.append(np.array(image.convert("RGB")))
    return frames


def build_palette(frame):
    """Read the colour bar of a frame -> (colors Nx3, dbz N) excluding the
    bottom (blue, <9.5 dBZ) swatch, which is indistinguishable from noise."""
    colors, dbz = [], []
    for k in range(BAR_SWATCHES - 1):
        y = int(BAR_Y_START + (k + 0.5) * BAR_SWATCH_PX)
        colors.append(frame[y, BAR_X].astype(int))
        dbz.append(DBZ_TOP - DBZ_STEP * k)
    return np.array(colors), np.array(dbz)


def dbz_field(frame, palette):
    """Per-pixel dBZ (0 where there is no echo) by colour matching, restricted to
    the radar circle."""
    colors, dbz_values = palette
    h, w, _ = frame.shape
    field = np.zeros((h, w), dtype=np.float32)

    # Cheap pre-filter: only vivid pixels can be echoes.
    spread = frame.max(axis=2).astype(int) - frame.min(axis=2)
    ys, xs = np.nonzero(spread >= MIN_ECHO_SPREAD)
    candidates = frame[ys, xs].astype(int)
    values = np.zeros(len(ys), dtype=np.float32)
    # Iterate strongest -> weakest so duplicated swatch colours resolve to the
    # weaker (more conservative) value.
    for color, dbz in zip(colors, dbz_values):
        if dbz < MIN_DBZ:
            continue
        match = (np.abs(candidates - color) <= COLOR_TOLERANCE).all(axis=1)
        values[match] = dbz
    field[ys, xs] = values

    yy, xx = np.ogrid[:h, :w]
    inside = (xx - CENTER_PX[0]) ** 2 + (yy - CENTER_PX[1]) ** 2 <= (RADAR_RANGE_KM / KM_PER_PX) ** 2
    field[~inside] = 0
    return field


def latlon_to_px(lat, lon):
    """(x, y) pixel of a coordinate in the frame (local flat-earth approximation)."""
    east_km = (lon - RADAR_LON) * 111.32 * math.cos(math.radians(RADAR_LAT))
    north_km = (lat - RADAR_LAT) * 110.57
    return (
        CENTER_PX[0] + east_km / KM_PER_PX,
        CENTER_PX[1] - north_km / KM_PER_PX,
    )


def _gaussian_spectrum(shape, sigma):
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.fftfreq(shape[1])[None, :]
    return np.exp(-2 * (np.pi * sigma) ** 2 * (fx ** 2 + fy ** 2))


def estimate_motion(prev, curr, center, interval_min=FRAME_INTERVAL_MIN):
    """Echo motion (dx_px, dy_px per frame interval) near `center`, from the
    cross-correlation of the two frames' (blurred) echo fields, or None if
    there isn't enough echo to trust it."""
    half = int(MOTION_WINDOW_KM / KM_PER_PX)
    cx, cy = int(round(center[0])), int(round(center[1]))
    x0, x1 = max(cx - half, 0), min(cx + half, curr.shape[1])
    y0, y1 = max(cy - half, 0), min(cy + half, curr.shape[0])
    a, b = prev[y0:y1, x0:x1], curr[y0:y1, x0:x1]

    if (a > 0).sum() < MOTION_MIN_ECHO_PX or (b > 0).sum() < MOTION_MIN_ECHO_PX:
        return None

    # Blur so cells that evolve between frames still overlap, and taper the
    # window edges so they don't create a fake zero-shift peak.
    window = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    blur = _gaussian_spectrum(a.shape, MOTION_BLUR_PX)
    fa = np.fft.fft2((a - a.mean()) * window) * blur
    fb = np.fft.fft2((b - b.mean()) * window) * blur
    corr = np.fft.fftshift(np.fft.ifft2(np.conj(fa) * fb).real)

    max_shift = int(MOTION_MAX_KMH * interval_min / 60 / KM_PER_PX)
    max_shift = min(max_shift, a.shape[0] // 2 - 1, a.shape[1] // 2 - 1)
    cy0, cx0 = a.shape[0] // 2, a.shape[1] // 2
    sub = corr[cy0 - max_shift:cy0 + max_shift + 1, cx0 - max_shift:cx0 + max_shift + 1]
    iy, ix = np.unravel_index(np.argmax(sub), sub.shape)
    return float(ix - max_shift), float(iy - max_shift)


def max_dbz_near(field, point_px, radius_km):
    """Strongest echo within radius_km of a pixel position (0 if none/out of frame)."""
    r = int(radius_km / KM_PER_PX)
    cx, cy = int(round(point_px[0])), int(round(point_px[1]))
    h, w = field.shape
    if not (0 <= cx < w and 0 <= cy < h):
        return 0.0
    x0, x1 = max(cx - r, 0), min(cx + r + 1, w)
    y0, y1 = max(cy - r, 0), min(cy + r + 1, h)
    yy, xx = np.ogrid[y0:y1, x0:x1]
    disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    patch = field[y0:y1, x0:x1]
    return float(patch[disc].max()) if disc.any() else 0.0


def _bearing_text(dx, dy):
    """Compass direction the echoes are coming FROM (Thai)."""
    # Echoes move by (dx, dy) in image coords (y down); they come from the opposite side.
    bearing = (math.degrees(math.atan2(-dx, dy)) + 360) % 360  # bearing of origin, 0 = N
    names = ["เหนือ", "ตะวันออกเฉียงเหนือ", "ตะวันออก", "ตะวันออกเฉียงใต้",
             "ใต้", "ตะวันตกเฉียงใต้", "ตะวันตก", "ตะวันตกเฉียงเหนือ"]
    return names[int((bearing + 22.5) // 45) % 8]


def prepare_radar(frames):
    """dBZ fields of the last two frames (the only ones the nowcast needs)."""
    palette = build_palette(frames[-1])
    return {
        "prev": dbz_field(frames[-2], palette) if len(frames) >= 2 else None,
        "curr": dbz_field(frames[-1], palette),
    }


def analyze_radar(prepared, lat, lon):
    """Radar nowcast for a point, from prepare_radar() output.

    Returns None if the point is outside radar coverage, else a dict:
      now_dbz, motion_kmh / from_direction (or None), eta_min (None if no rain
      expected within LEAD_MAX_MIN), peak_dbz (echo that will reach the point),
      triggered.
    """
    point = latlon_to_px(lat, lon)
    dist_km = math.hypot(point[0] - CENTER_PX[0], point[1] - CENTER_PX[1]) * KM_PER_PX
    if dist_km > RADAR_RANGE_KM - 15:
        return None

    curr = prepared["curr"]
    now_dbz = max_dbz_near(curr, point, TARGET_RADIUS_KM)

    motion = None
    if prepared["prev"] is not None:
        motion = estimate_motion(prepared["prev"], curr, point)

    eta_min, peak_dbz = None, 0.0
    if now_dbz >= RADAR_DBZ_THRESHOLD:
        eta_min, peak_dbz = 0, now_dbz
    elif motion is not None:
        # Walk upwind in steps no longer than the search radius so fast-moving
        # cells can't slip between samples.
        intervals = LEAD_MAX_MIN / FRAME_INTERVAL_MIN
        path_px = math.hypot(*motion) * intervals
        radius_px = TARGET_RADIUS_KM / KM_PER_PX
        steps = max(6, math.ceil(path_px / radius_px))
        for i in range(1, steps + 1):
            frac = intervals * i / steps
            upwind = (point[0] - motion[0] * frac, point[1] - motion[1] * frac)
            dbz = max_dbz_near(curr, upwind, TARGET_RADIUS_KM)
            if dbz >= RADAR_DBZ_THRESHOLD:
                eta_min = max(5, int(round(frac * FRAME_INTERVAL_MIN / 5)) * 5)
                peak_dbz = dbz
                break

    result = {
        "now_dbz": now_dbz,
        "peak_dbz": peak_dbz,
        "eta_min": eta_min,
        "motion_kmh": None,
        "from_direction": None,
        "triggered": eta_min is not None,
    }
    if motion is not None:
        result["motion_kmh"] = math.hypot(*motion) * KM_PER_PX * 60 / FRAME_INTERVAL_MIN
        if result["motion_kmh"] >= 3:
            result["from_direction"] = _bearing_text(*motion)
    return result


def fetch_radar_size():
    """Size in bytes of the radar GIF (HEAD request), or None. Used to detect new frames."""
    try:
        response = requests.head(RADAR_GIF_URL, timeout=15, allow_redirects=True)
        response.raise_for_status()
        return int(response.headers["Content-Length"])
    except (requests.RequestException, KeyError, ValueError) as exc:
        logger.warning("Could not read radar loop size: %s", exc)
        return None


def fetch_radar():
    """Download the radar loop and prepare it for analyze_radar().
    Returns None on any failure so radar problems never break the alert run."""
    try:
        logger.info("Fetching TMD radar loop...")
        response = requests.get(RADAR_GIF_URL, timeout=RADAR_DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        frames = load_frames(response.content)
        logger.info("Radar loop fetched (%d frames).", len(frames))
        return prepare_radar(frames)
    except Exception as exc:  # noqa: BLE001 - radar is optional; log and carry on
        logger.warning("Could not fetch/decode radar loop: %s", exc)
        return None
