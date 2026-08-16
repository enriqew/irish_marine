#!/usr/bin/env python3
"""
render_sst_tiles.py — Render the OISST field as colour PNG tiles for the map's
temperature surface (a Leaflet ImageOverlay), instead of coarse grid rectangles.

Why raster
----------
The old backdrop drew the OBIS density grid (986 cells, 11.25°×5.625°) as filled
rectangles: huge blocks, ragged coastlines, and ocean gaps wherever OBIS had no
records. The SST field should come from OISST itself, which covers the WHOLE
ocean and marks land as NaN — so rasterising it gives pixel-crisp coastlines
(land = transparent), full ocean coverage, and a smooth surface the browser
interpolates for free. One small PNG per month animates by swapping the image.

Colour ramp is the frontend's RdYlBu-reversed SST ramp, kept in sync by hand
(SST_MIN/MAX and stops mirror src/components/MarineAtlas/data.ts).

Input : data/oisst_grid.npz   (from fetch_oisst.py)
Output: output/sst/<YYYY-MM>.png   one per month (land transparent)
        output/sst/mean.png        all-time mean (overview / no month selected)
        output/sst/field.json      meta: months, overlay bounds, temp range

No third-party imaging dependency — PNGs are written with a tiny pure-numpy/zlib
encoder (RGBA, 8-bit).
"""

import json
import math
import struct
import zlib
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "sst"

# Web Mercator (EPSG:3857) latitude limit — the basemap's own clamp. Tiles are
# reprojected to Mercator so they align pixel-for-pixel with the CARTO basemap
# (an equirectangular image placed by lat/lng would smear at the poles and drift
# off the coastlines — the "two mismatched maps" look).
MERC_LAT = 85.05112878
# Output raster size (square, since full Mercator is square). Bumped to 1536 for the
# native 0.25° source so its finer structure is not thrown away and the field stays
# crisp when the globe is zoomed.
OUT_W = 1536
OUT_H = 1536

# --- SST colour ramp (mirror of data.ts tempToColor) -----------------------
SST_MIN, SST_MAX = -2.0, 30.0
STOP_T = np.array([-2, 4, 10, 15, 20, 26, 30], dtype="float64")
STOP_RGB = np.array([
    [49, 54, 149], [69, 117, 180], [116, 173, 209], [171, 217, 233],
    [254, 224, 144], [244, 109, 67], [165, 0, 38],
], dtype="float64")


def _colorize(field: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """(H,W) float SST + (H,W) 0..1 coverage → (H,W,4) uint8 RGBA. `field` is
    land-filled so RGB stays sensible under semi-transparent coastal pixels;
    `alpha` carries the antialiased ocean mask (land fully transparent)."""
    v = np.clip(field, SST_MIN, SST_MAX)
    r = np.interp(v, STOP_T, STOP_RGB[:, 0])
    g = np.interp(v, STOP_T, STOP_RGB[:, 1])
    b = np.interp(v, STOP_T, STOP_RGB[:, 2])
    a = np.clip(alpha, 0.0, 1.0)
    rgba = np.zeros((*field.shape, 4), dtype="uint8")
    rgba[..., 0] = r.astype("uint8")
    rgba[..., 1] = g.astype("uint8")
    rgba[..., 2] = b.astype("uint8")
    rgba[..., 3] = np.round(a * 255).astype("uint8")
    # keep RGB at 0 where fully transparent (smaller PNG, no stray colours)
    rgba[a <= 0.0, :3] = 0
    return rgba


def _fill_land(field: np.ndarray, iters: int = 6) -> np.ndarray:
    """Diffuse ocean SST a few cells into the land (NaN) so that bilinear
    interpolation near coastlines doesn't dip toward NaN/0 — the mask is
    re-applied afterwards, so this only affects sub-pixel coastal blending."""
    T = field.astype("float64").copy()
    for _ in range(iters):
        nan = ~np.isfinite(T)
        if not nan.any():
            break
        acc = np.zeros_like(T)
        cnt = np.zeros_like(T)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            vals = np.roll(np.where(np.isfinite(T), T, 0.0), (dy, dx), axis=(0, 1))
            has = np.roll(np.isfinite(T).astype("float64"), (dy, dx), axis=(0, 1))
            acc += vals
            cnt += has
        fill = np.divide(acc, cnt, out=np.full_like(T, np.nan), where=cnt > 0)
        T = np.where(nan & (cnt > 0), fill, T)
    return np.where(np.isfinite(T), T, np.nanmean(field))


def _lerp_axis(A: np.ndarray, idx: np.ndarray, axis: int) -> np.ndarray:
    """Linear resample of `A` along `axis` at fractional positions `idx`."""
    n = A.shape[axis]
    i0 = np.floor(idx).astype(int)
    i0 = np.clip(i0, 0, n - 1)
    i1 = np.clip(i0 + 1, 0, n - 1)
    f = (idx - i0).astype("float64")
    a0 = np.take(A, i0, axis=axis)
    a1 = np.take(A, i1, axis=axis)
    shape = [1] * A.ndim
    shape[axis] = len(idx)
    return a0 * (1 - f.reshape(shape)) + a1 * f.reshape(shape)


SUPERSAMPLE = 4   # mask oversampling factor for antialiased coastlines


def _to_mercator(field_asc: np.ndarray, lats_asc: np.ndarray,
                 lons_asc: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Reproject an equirectangular field (rows = ascending lat, cols = ascending
    lon) onto a Web-Mercator raster (row 0 = north, OUT_H×OUT_W) with **bilinear**
    resampling for a continuous gradient. Returns `(field, alpha)`.

    The old hard cut (`mask >= 0.5` at output resolution) drew the coastline as a
    0.25° staircase — visibly "Lego" when the globe is zoomed. Instead the ocean
    mask is resampled at SUPERSAMPLE× resolution, thresholded there (the 0.5
    isoline of the bilinear surface is a smooth curve, not a staircase), and
    box-averaged back down — an antialiased alpha edge that follows the data's
    real coastline shape. Land is filled before interpolation so RGB under the
    semi-transparent edge pixels stays sensible."""
    filled = _fill_land(field_asc)
    mask = np.isfinite(field_asc).astype("float32")

    # Fractional source column for each output column (lon is linear in Mercator).
    col_idx = np.linspace(0, field_asc.shape[1] - 1, OUT_W)
    col_idx_ss = np.linspace(0, field_asc.shape[1] - 1, OUT_W * SUPERSAMPLE)
    filled = _lerp_axis(filled, col_idx, axis=1)
    mask = _lerp_axis(mask, col_idx_ss, axis=1)

    # Fractional source row for each output row (Mercator y → latitude → row).
    y_max = math.log(math.tan(math.pi / 4 + math.radians(MERC_LAT) / 2))
    ys = np.linspace(y_max, -y_max, OUT_H)
    ys_ss = np.linspace(y_max, -y_max, OUT_H * SUPERSAMPLE)
    lats_out = np.degrees(2.0 * np.arctan(np.exp(ys)) - math.pi / 2)   # north→south
    lats_out_ss = np.degrees(2.0 * np.arctan(np.exp(ys_ss)) - math.pi / 2)
    row_idx = np.interp(lats_out, lats_asc, np.arange(len(lats_asc)))
    row_idx_ss = np.interp(lats_out_ss, lats_asc, np.arange(len(lats_asc)))
    filled = _lerp_axis(filled, row_idx, axis=0)
    mask = _lerp_axis(mask, row_idx_ss, axis=0)

    # Threshold at supersampled resolution, then box-average down → antialiased
    # 0..1 coverage per output pixel.
    hard = (mask >= 0.5).astype("float32")
    alpha = hard.reshape(OUT_H, SUPERSAMPLE, OUT_W, SUPERSAMPLE).mean(axis=(1, 3))
    return filled, alpha


def _write_png(path: Path, rgba: np.ndarray) -> None:
    """Minimal RGBA PNG writer (stdlib zlib only)."""
    h, w, _ = rgba.shape
    raw = bytearray()
    row_bytes = rgba.reshape(h, w * 4)
    for y in range(h):
        raw.append(0)                     # filter type 0 (none)
        raw.extend(row_bytes[y].tobytes())
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8-bit, RGBA
    idat = zlib.compress(bytes(raw), 6)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def main() -> None:
    grid_path = DATA_DIR / "oisst_grid.npz"
    if not grid_path.exists():
        raise SystemExit("data/oisst_grid.npz not found — run fetch_oisst.py first")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    z = np.load(grid_path, allow_pickle=True)
    months = [str(m) for m in z["months"]]
    lats = z["lats"].astype("float64")
    lons = z["lons"].astype("float64")
    sst = z["sst"].astype("float32")                    # (T, Y, X)

    # Ascending lat/lon so the Mercator remap can index rows/cols directly.
    lon_order = np.argsort(lons)
    lat_order = np.argsort(lats)
    lons = lons[lon_order]
    lats = lats[lat_order]
    sst = sst[:, lat_order, :][:, :, lon_order]

    # Global field: overlay spans the full world horizontally and the Mercator
    # latitude clamp vertically (matches the basemap's extent exactly).
    bounds = [[-MERC_LAT, -180.0], [MERC_LAT, 180.0]]

    print(f"Rendering {len(months)} Mercator tiles {OUT_W}×{OUT_H} "
          f"from a {sst.shape[2]}×{sst.shape[1]} source...")
    for t, ym in enumerate(months):
        field, alpha = _to_mercator(sst[t], lats, lons)
        _write_png(OUT_DIR / f"{ym}.png", _colorize(field, alpha))
        if (t + 1) % 24 == 0 or t == len(months) - 1:
            print(f"  {t + 1}/{len(months)}", end="\r", flush=True)

    # All-time mean (overview when no month is selected).
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mean_field = np.nanmean(sst, axis=0)
    mean_merc, mean_alpha = _to_mercator(mean_field, lats, lons)
    _write_png(OUT_DIR / "mean.png", _colorize(mean_merc, mean_alpha))

    meta = {
        "months": months,
        "bounds": bounds,                # [[south, west], [north, east]], Web Mercator clamp
        "projection": "epsg3857",
        "temp_min": SST_MIN,
        "temp_max": SST_MAX,
        "width": OUT_W,
        "height": OUT_H,
    }
    (OUT_DIR / "field.json").write_text(json.dumps(meta), encoding="utf-8")

    total_kb = sum(f.stat().st_size for f in OUT_DIR.glob("*.png")) / 1024
    print(f"\n  Wrote {len(months) + 1} PNGs + field.json to {OUT_DIR} "
          f"({total_kb / 1024:.1f} MB total)")


if __name__ == "__main__":
    main()
