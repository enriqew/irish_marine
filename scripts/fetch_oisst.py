#!/usr/bin/env python3
"""
fetch_oisst.py — Build the NOAA OISST v2.1 *monthly* SST field GLOBALLY at native
**0.25°** from the daily files on **AWS Open Data** (no NOAA PSL, no rate limits),
and save it as a compact NumPy grid for the SST join in process.py.

Why AWS, not NOAA PSL OPeNDAP
----------------------------
The pre-aggregated monthly file lives on NOAA PSL's THREDDS/OPeNDAP server, which
**rate-limits hard (HTTP 429)** — unusable when it throttles. NOAA's OISST v2.1 daily
files are also mirrored on AWS Open Data (anonymous S3, no limits):

  s3://noaa-cdr-sea-surface-temp-optimum-interpolation-pds/data/v2.1/avhrr/<YYYYMM>/
      oisst-avhrr-v02r01.<YYYYMMDD>.nc     (daily, global 0.25°, sst in °C)

There is no monthly aggregate there, so we build monthly means ourselves. Averaging a
handful of days per month is plenty for a smooth SST *surface* (the day-to-day standard
error of a ~10-day mean is well under 0.1 °C for this use — a colour field, not a
climate record), so we sample every ``DAY_STRIDE``-th day rather than pulling all ~30.

Output: data/oisst_grid.npz  — arrays: months (str), lats, lons, sst[T, Y, X] (°C).

Licence: OISST is a U.S. Government work in the public domain. See DATA_LICENSE.md.
"""

import calendar
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import s3fs
import xarray as xr

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

BUCKET = "noaa-cdr-sea-surface-temp-optimum-interpolation-pds"
DAILY_PREFIX = f"{BUCKET}/data/v2.1/avhrr"     # <YYYYMM>/oisst-avhrr-v02r01.<YYYYMMDD>.nc

START_MONTH = "2015-01"   # timeline start
DAY_STRIDE = 3            # sample every 3rd day of each month for the monthly mean
DL_WORKERS = 16           # parallel S3 downloads


def _months(start_ym: str, end_ym: str) -> list[str]:
    sy, sm = (int(x) for x in start_ym.split("-"))
    ey, em = (int(x) for x in end_ym.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _sampled_keys(fs: s3fs.S3FileSystem, ym: str) -> list[str]:
    """Every DAY_STRIDE-th daily .nc key for a month (empty if the month is absent)."""
    y, m = (int(x) for x in ym.split("-"))
    prefix = f"{DAILY_PREFIX}/{y}{m:02d}"
    try:
        keys = [k for k in fs.ls(prefix) if k.endswith(".nc")]
    except FileNotFoundError:
        return []
    keys.sort()
    return keys[::DAY_STRIDE]


def _download(fs: s3fs.S3FileSystem, keys: list[str], dest: Path) -> list[Path]:
    """Fetch keys to dest in parallel; return local paths (skips any that fail)."""
    def one(k: str) -> Path | None:
        p = dest / k.split("/")[-1]
        try:
            fs.get(k, str(p))
            return p
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=DL_WORKERS) as pool:
        return [p for p in pool.map(one, keys) if p is not None]


def _monthly_mean(paths: list[Path]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """NaN-aware mean SST over the given daily files → (sst[Y,X] °C, lats, lons).
    Each file is opened in a context manager and its array copied out, so no handle
    lingers (Windows can't delete a still-open netCDF)."""
    acc = cnt = lats = lons = None
    for p in paths:
        with xr.open_dataset(str(p), engine="netcdf4", decode_times=False) as ds:
            da = ds["sst"]
            if "zlev" in da.dims:
                da = da.isel(zlev=0)
            arr = np.asarray(da.values, dtype="float32").squeeze()   # (Y, X) copy
            if lats is None:
                lats = ds["lat"].values.astype("float32")
                lons = ds["lon"].values.astype("float32")
        valid = np.isfinite(arr)
        if acc is None:
            acc = np.where(valid, arr, 0.0)
            cnt = valid.astype("float32")
        else:
            acc += np.where(valid, arr, 0.0)
            cnt += valid
    mean = np.divide(acc, cnt, out=np.full_like(acc, np.nan), where=cnt > 0)
    return mean, lats, lons


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fs = s3fs.S3FileSystem(anon=True)

    end_month = date.today().strftime("%Y-%m")
    months = _months(START_MONTH, end_month)
    print(f"Fetching NOAA OISST v2.1 monthly SST — GLOBAL 0.25° via AWS Open Data "
          f"({len(months)} months, ~{31 // DAY_STRIDE + 1} days/month sampled)...")

    lats = lons = None
    fields: list[np.ndarray] = []
    kept_months: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for i, ym in enumerate(months, 1):
            keys = _sampled_keys(fs, ym)
            if not keys:
                print(f"  {ym}: no data — skipping", flush=True)
                continue
            for f in tmp_dir.glob("*.nc"):
                f.unlink()
            paths = _download(fs, keys, tmp_dir)
            if not paths:
                print(f"  {ym}: downloads failed — skipping", flush=True)
                continue
            field, la, lo = _monthly_mean(paths)
            if lats is None:
                lats, lons = la, lo
            fields.append(field)
            kept_months.append(ym)
            if i % 12 == 0 or i == len(months):
                print(f"  {i}/{len(months)}  {ym}  ({len(paths)} days)", flush=True)

    if not fields:
        raise SystemExit("no OISST months fetched from AWS")

    values = np.stack(fields, axis=0)   # (T, Y, X)

    # Physical sanity: clamp obviously bad values to NaN (land already NaN).
    values[(values < -2.5) | (values > 40)] = np.nan

    # Longitudes are 0-360 in OISST; convert to -180/180 and re-sort ascending.
    lons = np.asarray(lons, dtype="float32")
    if float(lons.max()) > 180.0:
        lons = np.where(lons > 180, lons - 360, lons).astype("float32")
    order = np.argsort(lons)
    lons = lons[order]
    values = values[:, :, order]

    months_arr = np.array(kept_months)
    out_path = DATA_DIR / "oisst_grid.npz"
    np.savez_compressed(out_path, months=months_arr, lats=lats.astype("float32"),
                        lons=lons, sst=values)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    ocean = int(np.isfinite(values[0]).sum())
    print(f"\n  {len(kept_months)} months × {values.shape[1]}×{values.shape[2]} grid "
          f"(~{ocean} ocean cells/month)")
    print(f"  Saved -> {out_path}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
