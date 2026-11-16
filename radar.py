"""Rain-radar nowcasting from TMD high-quality radar loops.

Each station's "Loop (240)" page shows one 1920x1600 GIF (12 frames, 15
minutes apart, 240 km radius). The page draws it at 800 px and the Zoom
buttons only scale that same image. A query uses the nearest station whose
circle covers the point, so a location outside Suvarnabhumi still gets a
radar check. Stations whose files are a different size are left out: the
pixel geometry would not match and the rain would be placed in the wrong spot.

Frames are plain images, so we:

1. classify echo pixels by matching them against the colour bar drawn on each
   frame (colour -> dBZ),
2. estimate how the echoes are moving (cross-correlation of the last two
   frames), and
3. look "upwind" of the target point to see whether rain is there now or is
   about to arrive within the next hour.

Suvarnabhumi's map calibration was fitted against its crosshair and colour
bar. The other 1920x1600 stations share that layout; their lat/lon are the
published radar positions. ETAs are approximate (a few km / minutes).

Callers judge freshness by when the file's content last changed (see `digest`).
"""

import hashlib
import io
import logging
import math

import numpy as np
import requests
from PIL import Image, ImageDraw

logger = logging.getLogger("rain-alert")

RADAR_DOWNLOAD_TIMEOUT = (10, 40)  # (connect, read) seconds; one GIF is ~15 MB

# Shared geometry of the 1920x1600 high-quality 240 km frames.
_HQ240 = {
    "center": (960, 800),
    "km_per_px": 0.3,
    "range_km": 235,  # inside the 240 km ring
    "frame_min": 15,
    "bar_x": 30,
    "bar_y": 8,
    "bar_swatch": 63.36,
}


def _station(sid, name, lat, lon, path):
    site = dict(_HQ240)
    site.update(id=sid, name=name, lat=lat, lon=lon, url="https://weather.tmd.go.th/" + path)
    return site


# One loop per station: the 240 km high-quality GIF, and only where that file
# is the standard 1920x1600 chart. Suvarnabhumi's lat/lon stay on the fitted
# crosshair; the rest use the published radar coordinates.
STATIONS = [
    _station("svp", "สุวรรณภูมิ", 13.69, 100.76, "svp/svpHQloop.gif"),
    _station("skm", "สมุทรสงคราม", 13.407222, 100.031944, "skm/skmHQloop.gif"),
    _station("chn", "ชัยนาท", 15.157853, 100.191264, "chn/chnHQloop.gif"),
    _station("kkw", "นครนายก", 14.362222, 101.393061, "kkw/kkwHQloop.gif"),
    _station("cri", "เชียงราย", 19.961389, 99.881383, "cri/criHQloop.gif"),
    _station("lmp", "ลำพูน", 18.5654, 99.0417, "lmp/lmpHQloop.gif"),
    _station("mhs", "แม่ฮ่องสอน", 19.301, 97.969, "mhs/mhsHQloop.gif"),
    _station("tak", "ดอยมูเซอ", 16.752, 98.935, "tak/takHQloop.gif"),
    _station("phs", "พิษณุโลก", 16.775361, 100.217886, "phs/phsHQloop.gif"),
    _station("kkn", "ขอนแก่น", 16.4625, 102.785881, "kkn/kknHQloop.gif"),
    _station("skn", "สกลนคร", 17.156364, 104.132592, "skn/sknHQloop.gif"),
    _station("ubn", "อุบลราชธานี", 15.243611, 104.874728, "ubn/ubnHQloop.gif"),
    _station("srn", "สุรินทร์", 14.875928, 103.495908, "srn/srnHQloop.gif"),
    _station("cmp", "ชุมพร", 10.4931, 99.188203, "cmp/cmpHQloop.gif"),
    _station("srt", "สุราษฎร์ธานี", 9.135556, 99.15195, "srt/srtHQloop.gif"),
    _station("pkt", "ภูเก็ต", 8.133611, 98.329444, "pkt/pktHQloop.gif"),
    _station("stp", "สทิงพระ", 7.449994, 100.459994, "stp/stpHQloop.gif"),
    _station("hyi", "หาดใหญ่", 6.933, 100.393, "hyi/hyiHQloop.gif"),
    _station("nrt", "นราธิวาส", 6.426667, 101.825164, "nrt/nrtHQloop.gif"),
]
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
SNAPSHOT_HALF_KM = 60
SNAPSHOT_SIZE_PX = 800
SNAPSHOT_MARKER = (255, 0, 255)



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


def distance_km(lat1, lon1, lat2, lon2):
    """Flat-earth distance in km, fine for a few hundred kilometres."""
    east = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    north = (lat2 - lat1) * 110.57
    return math.hypot(east, north)


def stations_covering(lat, lon):
    """Stations whose radar circle contains the point, nearest first."""
    covering = []
    for site in STATIONS:
        distance = distance_km(lat, lon, site["lat"], site["lon"])
        if distance <= site["range_km"] - 15:
            covering.append((distance, site))
    covering.sort(key=lambda item: item[0])
    return [site for _, site in covering]


def build_palette(frame, site):
    """Read the colour bar of a frame -> (colors Nx3, dbz N) excluding the
    bottom (blue, <9.5 dBZ) swatch, which is indistinguishable from noise."""
    colors, dbz = [], []
    for k in range(BAR_SWATCHES - 1):
        y = int(site["bar_y"] + (k + 0.5) * site["bar_swatch"])
        colors.append(frame[y, site["bar_x"]].astype(int))
        dbz.append(DBZ_TOP - DBZ_STEP * k)
    return np.array(colors), np.array(dbz)


def dbz_field(frame, palette, site):
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

    cx, cy = site["center"]
    radius_px = site["range_km"] / site["km_per_px"]
    yy, xx = np.ogrid[:h, :w]
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= radius_px ** 2
    field[~inside] = 0
    return field


def latlon_to_px(lat, lon, site):
    """(x, y) pixel of a coordinate in the frame (local flat-earth approximation)."""
    east_km = (lon - site["lon"]) * 111.32 * math.cos(math.radians(site["lat"]))
    north_km = (lat - site["lat"]) * 110.57
    cx, cy = site["center"]
    return (
        cx + east_km / site["km_per_px"],
        cy - north_km / site["km_per_px"],
    )


def _gaussian_spectrum(shape, sigma):
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.fftfreq(shape[1])[None, :]
    return np.exp(-2 * (np.pi * sigma) ** 2 * (fx ** 2 + fy ** 2))


def estimate_motion(prev, curr, center, site):
    """Echo motion (dx_px, dy_px per frame interval) near `center`, from the
    cross-correlation of the two frames' (blurred) echo fields, or None if
    there isn't enough echo to trust it."""
    half = int(MOTION_WINDOW_KM / site["km_per_px"])
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

    max_shift = int(MOTION_MAX_KMH * site["frame_min"] / 60 / site["km_per_px"])
    max_shift = min(max_shift, a.shape[0] // 2 - 1, a.shape[1] // 2 - 1)
    cy0, cx0 = a.shape[0] // 2, a.shape[1] // 2
    sub = corr[cy0 - max_shift:cy0 + max_shift + 1, cx0 - max_shift:cx0 + max_shift + 1]
    iy, ix = np.unravel_index(np.argmax(sub), sub.shape)
    return float(ix - max_shift), float(iy - max_shift)


def max_dbz_near(field, point_px, radius_km, site):
    """Strongest echo within radius_km of a pixel position (0 if none/out of frame)."""
    r = int(radius_km / site["km_per_px"])
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


def prepare_radar(frames, site):
    """dBZ fields of the last two frames (the only ones the nowcast needs),
    plus the latest frame itself (for snapshots)."""
    palette = build_palette(frames[-1], site)
    return {
        "site": site,
        "prev": dbz_field(frames[-2], palette, site) if len(frames) >= 2 else None,
        "curr": dbz_field(frames[-1], palette, site),
        "image": frames[-1],
    }


def render_snapshot(prepared, lat, lon, motion_px=None):
    """JPEG bytes of the latest frame cropped around (lat, lon), with a marker
    on the point and, if known, an arrow showing where the rain comes from
    (the stretch the echoes cover in 30 minutes)."""
    site = prepared["site"]
    image = prepared["image"]
    h, w, _ = image.shape
    px, py = latlon_to_px(lat, lon, site)
    half = int(SNAPSHOT_HALF_KM / site["km_per_px"])
    x0 = min(max(int(px) - half, 0), w - 2 * half)
    y0 = min(max(int(py) - half, 0), h - 2 * half)
    crop = Image.fromarray(image[y0:y0 + 2 * half, x0:x0 + 2 * half])
    crop = crop.resize((SNAPSHOT_SIZE_PX, SNAPSHOT_SIZE_PX), Image.LANCZOS)

    scale = SNAPSHOT_SIZE_PX / (2 * half)
    cx, cy = (px - x0) * scale, (py - y0) * scale
    draw = ImageDraw.Draw(crop)
    if motion_px is not None and math.hypot(*motion_px) > 0:
        frac = 30 / site["frame_min"]
        sx = cx - motion_px[0] * frac * scale
        sy = cy - motion_px[1] * frac * scale
        draw.line((sx, sy, cx, cy), fill=SNAPSHOT_MARKER, width=4)
        draw.ellipse((sx - 6, sy - 6, sx + 6, sy + 6), fill=SNAPSHOT_MARKER)
    r = TARGET_RADIUS_KM / site["km_per_px"] * scale
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
    site = prepared["site"]
    point = latlon_to_px(lat, lon, site)
    cx, cy = site["center"]
    dist_km = math.hypot(point[0] - cx, point[1] - cy) * site["km_per_px"]
    if dist_km > site["range_km"] - 15:
        return None

    curr = prepared["curr"]
    motion = None
    if prepared["prev"] is not None:
        motion = estimate_motion(prepared["prev"], curr, point, site)

    def dbz_arriving_after(minutes):
        """Echo reaching the point `minutes` after the frame time."""
        if motion is None:
            return max_dbz_near(curr, point, TARGET_RADIUS_KM, site)
        frac = minutes / site["frame_min"]
        upwind = (point[0] - motion[0] * frac, point[1] - motion[1] * frac)
        return max_dbz_near(curr, upwind, TARGET_RADIUS_KM, site)

    age_min = max(age_min, 0)
    now_dbz = dbz_arriving_after(age_min)
    eta_min, peak_dbz = None, 0.0
    if now_dbz >= RADAR_DBZ_THRESHOLD:
        eta_min, peak_dbz = 0, now_dbz
    elif motion is not None:
        # Walk upwind in steps no longer than the search radius so fast-moving
        # cells can't slip between samples.
        path_px = math.hypot(*motion) * LEAD_MAX_MIN / site["frame_min"]
        radius_px = TARGET_RADIUS_KM / site["km_per_px"]
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
        result["motion_kmh"] = math.hypot(*motion) * site["km_per_px"] * 60 / site["frame_min"]
        if result["motion_kmh"] >= 3:
            result["from_direction"] = _bearing_text(*motion)
    return result


def fetch_radar(site):
    """Download one station's radar loop and prepare it for analyze_radar().
    Returns None on any failure so radar problems never break the alert run."""
    try:
        logger.info("Fetching TMD radar loop for %s...", site["id"])
        response = requests.get(site["url"], timeout=RADAR_DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        frames = load_frames(response.content, keep_last=2)
        logger.info("Radar loop fetched for %s (%d frames).", site["id"], len(frames))
        prepared = prepare_radar(frames, site)
        # Fingerprint of the content: unchanged for long = TMD stopped updating.
        prepared["digest"] = hashlib.sha1(response.content).hexdigest()
        return prepared
    except Exception as exc:  # noqa: BLE001 - radar is optional; log and carry on
        logger.warning("Could not fetch/decode radar loop for %s: %s", site["id"], exc)
        return None
