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
from datetime import datetime, timezone

import numpy as np
import requests
from PIL import Image, ImageDraw

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

# Telegram snapshot: latest frame cropped around the point.
SNAPSHOT_HALF_KM = 75
SNAPSHOT_SIZE_PX = 800
SNAPSHOT_MARKER = (255, 0, 255)


# Frame timestamp, drawn bottom-right as "YYYY-MM-DD HH:MM:SS" in UTC. Digits
# sit at fixed x offsets and are read by matching against DIGIT_GLYPHS
# (binary bitmaps learned from real frames, see GLYPH_* for tolerances).
STAMP_ROWS = (1569, 1584)
STAMP_X0 = 1746
STAMP_DIGIT_X = (17, 25, 34, 42, 58, 66, 78, 86, 101, 108, 122, 130, 143, 151)  # YYYYMMDDHHMMSS
STAMP_WHITE = 200  # text is white; all channels above this
GLYPH_WIDTH = 9
GLYPH_SEARCH_PX = 3  # glyphs are anti-aliased at sub-pixel offsets
GLYPH_MAX_ERROR = 32  # mismatched pixels (of 15x9) beyond which a digit is unreadable
GLYPH_MIN_MARGIN = 4  # best match must beat the runner-up by this many pixels

DIGIT_GLYPHS = {
    "0": (
        ".........",
        "..##.....",
        ".#####...",
        "###.###..",
        "##...##..",
        "##...##..",
        "##...##..",
        "##...##..",
        "##...##..",
        "##...##..",
        "##...##..",
        "###.###..",
        ".#####...",
        "...#.....",
        ".........",
    ),
    "1": (
        ".........",
        ".........",
        "###.....#",
        "###....##",
        ".##....##",
        ".##....##",
        ".##....##",
        ".##...###",
        ".##....#.",
        ".##....##",
        ".##....##",
        ".##....##",
        ".##.....#",
        ".........",
        ".........",
    ),
    "2": (
        ".........",
        "..##.....",
        "######...",
        "##..###..",
        "#....##..",
        ".....##..",
        "....###..",
        "...###...",
        "...##....",
        "..##.....",
        ".##......",
        "#######..",
        "########.",
        ".........",
        ".........",
    ),
    "3": (
        ".........",
        ".##......",
        "#####....",
        "#..###..#",
        "#...##..#",
        "....##..#",
        ".####..##",
        ".####..##",
        "...###..#",
        "....##..#",
        "#...##..#",
        "#..###..#",
        "#####....",
        ".........",
        ".........",
    ),
    "4": (
        ".........",
        ".........",
        "...##....",
        "..###....",
        ".####...#",
        ".####...#",
        "##.##...#",
        "##.##...#",
        "#..##....",
        "#######..",
        "#######.#",
        "...##...#",
        "...##....",
        ".........",
        ".........",
    ),
    "5": (
        ".........",
        ".###.#...",
        ".######..",
        ".#####...",
        "###......",
        "#####....",
        "######...",
        "###.###..",
        ".....##..",
        ".....##..",
        "##...##..",
        "###.###..",
        ".#####...",
        ".........",
        ".........",
    ),
    "6": (
        ".........",
        ".........",
        "...####..",
        "..####...",
        ".###.....",
        ".##.##...",
        ".######..",
        ".###.###.",
        ".##...##.",
        ".##...##.",
        ".##...##.",
        ".###.###.",
        "..#####..",
        ".........",
        ".........",
    ),
    "7": (
        ".........",
        "....##...",
        "########.",
        "......##.",
        "......##.",
        ".....##..",
        ".....##..",
        "....##...",
        "....##...",
        "...##....",
        "...##....",
        "..###....",
        "..##.....",
        ".........",
        ".........",
    ),
    "8": (
        ".........",
        "..##.....",
        ".#####...",
        "###.###..",
        "##...##..",
        "##...##..",
        "######...",
        ".#####...",
        "##..###..",
        "##...##..",
        "##...##..",
        "###.###..",
        ".#####...",
        ".........",
        ".........",
    ),
    "9": (
        ".........",
        "..##.....",
        ".#####...",
        "##..###..",
        "##...##..",
        "##...##..",
        "##...##..",
        "##..###..",
        "#######..",
        "..##.##..",
        "....##...",
        ".#####...",
        ".####....",
        ".........",
        ".........",
    ),
}


def dbz_to_rain_rate(dbz):
    """Marshall-Palmer style Z-R conversion, mm/h."""
    return (10 ** (dbz / 10) / 200) ** (1 / 1.6)


def load_frames(gif_bytes, keep_last=None):
    """Decode the GIF's frames into RGB numpy arrays (oldest first). GIF frames
    must be decoded in order, but only the last `keep_last` are kept in memory."""
    image = Image.open(io.BytesIO(gif_bytes))
    first = 0 if keep_last is None else max(image.n_frames - keep_last, 0)
    frames = []
    for index in range(image.n_frames):
        image.seek(index)
        if index >= first:
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


def _read_digit(text, x):
    """Best-matching digit for the glyph at column x, or None if unsure."""
    scores = []
    for digit, glyph in _GLYPH_ARRAYS.items():
        best = min(
            int((text[:, x + dx:x + dx + GLYPH_WIDTH] ^ glyph).sum())
            for dx in range(-GLYPH_SEARCH_PX, GLYPH_SEARCH_PX + 1)
        )
        scores.append((best, digit))
    scores.sort()
    if scores[0][0] > GLYPH_MAX_ERROR or scores[1][0] - scores[0][0] < GLYPH_MIN_MARGIN:
        return None
    return scores[0][1]


def read_frame_time(frame):
    """UTC timestamp printed on a frame, or None if it can't be read reliably."""
    try:
        text = frame[STAMP_ROWS[0]:STAMP_ROWS[1], STAMP_X0 - GLYPH_SEARCH_PX:].astype(int).min(axis=2) > STAMP_WHITE
        digits = [_read_digit(text, x + GLYPH_SEARCH_PX) for x in STAMP_DIGIT_X]
        if None in digits:
            return None
        return datetime.strptime("".join(digits), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None


def prepare_radar(frames):
    """dBZ fields of the last two frames (the only ones the nowcast needs),
    plus the latest frame's UTC time (None if unreadable)."""
    palette = build_palette(frames[-1])
    return {
        "prev": dbz_field(frames[-2], palette) if len(frames) >= 2 else None,
        "curr": dbz_field(frames[-1], palette),
        "time": read_frame_time(frames[-1]),
        "image": frames[-1],
    }


def render_snapshot(prepared, lat, lon, motion_px=None):
    """JPEG bytes of the latest frame cropped around (lat, lon), with a marker
    on the point and, if known, an arrow showing where the rain comes from
    (the stretch the echoes cover in 30 minutes)."""
    image = prepared["image"]
    h, w, _ = image.shape
    px, py = latlon_to_px(lat, lon)
    half = int(SNAPSHOT_HALF_KM / KM_PER_PX)
    x0 = min(max(int(px) - half, 0), w - 2 * half)
    y0 = min(max(int(py) - half, 0), h - 2 * half)
    crop = Image.fromarray(image[y0:y0 + 2 * half, x0:x0 + 2 * half])
    crop = crop.resize((SNAPSHOT_SIZE_PX, SNAPSHOT_SIZE_PX), Image.LANCZOS)

    scale = SNAPSHOT_SIZE_PX / (2 * half)
    cx, cy = (px - x0) * scale, (py - y0) * scale
    draw = ImageDraw.Draw(crop)
    if motion_px is not None and math.hypot(*motion_px) > 0:
        frac = 30 / FRAME_INTERVAL_MIN
        sx = cx - motion_px[0] * frac * scale
        sy = cy - motion_px[1] * frac * scale
        draw.line((sx, sy, cx, cy), fill=SNAPSHOT_MARKER, width=4)
        draw.ellipse((sx - 6, sy - 6, sx + 6, sy + 6), fill=SNAPSHOT_MARKER)
    r = TARGET_RADIUS_KM / KM_PER_PX * scale
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=SNAPSHOT_MARKER, width=4)
    draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=SNAPSHOT_MARKER)

    out = io.BytesIO()
    crop.save(out, format="JPEG", quality=85)
    return out.getvalue()


def analyze_radar(prepared, lat, lon, age_min=0):
    """Radar nowcast for a point, from prepare_radar() output.

    `age_min` is how old the latest frame is; echoes are extrapolated forward
    by that much so "now" and the ETA are relative to the real current time,
    not the frame time.

    Returns None if the point is outside radar coverage, else a dict:
      now_dbz, motion_kmh / from_direction (or None), eta_min (minutes from now,
      None if no rain expected within LEAD_MAX_MIN), peak_dbz (echo that will
      reach the point), triggered.
    """
    point = latlon_to_px(lat, lon)
    dist_km = math.hypot(point[0] - CENTER_PX[0], point[1] - CENTER_PX[1]) * KM_PER_PX
    if dist_km > RADAR_RANGE_KM - 15:
        return None

    curr = prepared["curr"]
    motion = None
    if prepared["prev"] is not None:
        motion = estimate_motion(prepared["prev"], curr, point)

    def dbz_arriving_after(minutes):
        """Echo reaching the point `minutes` after the frame time."""
        if motion is None:
            return max_dbz_near(curr, point, TARGET_RADIUS_KM)
        frac = minutes / FRAME_INTERVAL_MIN
        upwind = (point[0] - motion[0] * frac, point[1] - motion[1] * frac)
        return max_dbz_near(curr, upwind, TARGET_RADIUS_KM)

    age_min = max(age_min, 0)
    now_dbz = dbz_arriving_after(age_min)
    eta_min, peak_dbz = None, 0.0
    if now_dbz >= RADAR_DBZ_THRESHOLD:
        eta_min, peak_dbz = 0, now_dbz
    elif motion is not None:
        # Walk upwind in steps no longer than the search radius so fast-moving
        # cells can't slip between samples.
        path_px = math.hypot(*motion) * LEAD_MAX_MIN / FRAME_INTERVAL_MIN
        radius_px = TARGET_RADIUS_KM / KM_PER_PX
        steps = max(6, math.ceil(path_px / radius_px))
        for i in range(1, steps + 1):
            lead = LEAD_MAX_MIN * i / steps
            dbz = dbz_arriving_after(age_min + lead)
            if dbz >= RADAR_DBZ_THRESHOLD:
                eta_min = max(5, int(round(lead / 5)) * 5)
                peak_dbz = dbz
                break

    result = {
        "now_dbz": now_dbz,
        "peak_dbz": peak_dbz,
        "eta_min": eta_min,
        "motion_px": motion,
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
        frames = load_frames(response.content, keep_last=2)
        logger.info("Radar loop fetched (%d frames).", len(frames))
        return prepare_radar(frames)
    except Exception as exc:  # noqa: BLE001 - radar is optional; log and carry on
        logger.warning("Could not fetch/decode radar loop: %s", exc)
        return None


_GLYPH_ARRAYS = {
    digit: np.array([[ch == "#" for ch in row] for row in rows])
    for digit, rows in DIGIT_GLYPHS.items()
}
