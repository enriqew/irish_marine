#!/usr/bin/env python3
"""
fetch_oisst.py — Download the NOAA OISST v2.1 *monthly* SST field GLOBALLY, at
~1° resolution, and save it as a compact NumPy grid for the SST join in
process.py.

Source: NOAA/NCEI Daily Optimum Interpolation SST (OISST) v2.1, monthly-mean
aggregate served by NOAA PSL over OPeNDAP:
  https://psl.noaa.gov/thredds/dodsC/Datasets/noaa.oisst.v2.highres/sst.mon.mean.nc

The native grid is global 0.25° (720×1440 ≈ 1 M cells) — ~540 MB over the wire
for the full monthly time range, which is wasteful for a 1°-ish dashboard.
OPeNDAP supports *strided* access, so we take every 4th cell (``lat``/``lon``
step 4 → ~1° sampling, ~1/16 the data ≈ 35 MB). A temperature field is smooth
enough that point-sampling every 4th cell is fine for this purpose.

We keep the whole GLOBAL field (not a bounding box) so process.py can sample SST
at any species' cells, anywhere in the ocean.

Output: data/oisst_grid.npz  — arrays: months (str), lats, lons, sst[T, Y, X].

Licence: OISST is a U.S. Government work in the public domain. See DATA_LICENSE.md.
"""

from datetime import date
from pathlib import Path

import numpy as np
import xarray as xr

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PSL_OPENDAP = (
    "https://psl.noaa.gov/thredds/dodsC/"
    "Datasets/noaa.oisst.v2.highres/sst.mon.mean.nc"
)

START_MONTH = "2015-01"   # timeline start; end clamps to dataset availability
STRIDE = 4                # every 4th 0.25° cell → ~1° sampling
TIME_CHUNK = 12           # load this many months per OPeNDAP request

# NOTE: a single strided request for the whole time range (~35 MB) silently
# returns all-zeros from the THREDDS server — it must be loaded in time chunks.


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching NOAA OISST v2.1 monthly SST — GLOBAL, ~1° (strided) via NOAA PSL...")
    ds = xr.open_dataset(PSL_OPENDAP)
    try:
        end_month = date.today().strftime("%Y-%m")
        lazy = ds["sst"].sel(time=slice(f"{START_MONTH}-01", f"{end_month}-28"))
        # Strided global subset — every STRIDE-th 0.25° cell → ~1° sampling.
        lazy = lazy.isel(lat=slice(0, None, STRIDE), lon=slice(0, None, STRIDE))
        n_months = lazy.sizes.get("time", 0)
        print(f"  {n_months} months ({START_MONTH}..{end_month}), "
              f"grid {lazy.sizes.get('lat')}×{lazy.sizes.get('lon')} — "
              f"loading in {TIME_CHUNK}-month chunks over OPeNDAP...")

        # Load month-chunks and stack (a single whole-range request returns zeros).
        parts = []
        for start in range(0, n_months, TIME_CHUNK):
            part = lazy.isel(time=slice(start, start + TIME_CHUNK)).load()
            parts.append(part.values.astype("float32"))
            print(f"    {min(start + TIME_CHUNK, n_months)}/{n_months} months", end="\r", flush=True)
        values = np.concatenate(parts, axis=0)  # (time, lat, lon)

        months = np.array([str(t)[:7] for t in lazy["time"].values])  # 'YYYY-MM'
        lats = lazy["lat"].values.astype("float32")
        # OISST longitudes are 0-360; convert to -180/180 and re-sort ascending.
        lons_360 = lazy["lon"].values
        lons = np.where(lons_360 > 180, lons_360 - 360, lons_360).astype("float32")
        order = np.argsort(lons)
        lons = lons[order]
        values = values[:, :, order]
    finally:
        ds.close()

    # Physical sanity: clamp obviously bad values to NaN (land is already NaN).
    values[(values < -2.5) | (values > 40)] = np.nan

    out_path = DATA_DIR / "oisst_grid.npz"
    np.savez_compressed(out_path, months=months, lats=lats, lons=lons, sst=values)
    size_mb = out_path.stat().st_size / (1024 * 1024)
    ocean = int(np.isfinite(values[0]).sum())
    print(f"  {len(months)} months × {values.shape[1]}×{values.shape[2]} grid "
          f"(~{ocean} ocean cells/month)")
    print(f"  Saved -> {out_path}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
