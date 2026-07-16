#!/usr/bin/env python3
"""
process.py — Join the OBIS species snapshot with the global OISST field and emit
the Marine Atlas frontend artifacts.

Inputs (data/):
  obis_species.json  — curated species with per-cell footprint + annual counts
  oisst_grid.npz     — global ~1° monthly SST field

Outputs (output/):
  cells.json    — deduplicated grid-cell centroids; occurrences reference the index
  species.json  — catalog + per-species monthly SST series (seasonal, over its
                  range) + annual counts + thermal niche  (the correlation data)
  density.json  — global all-species per-cell totals + mean SST (idle backdrop)

The key computed quantity is, per species, the monthly mean SST *over the cells
where that species occurs* (count-weighted). That is the temperature-as-temporal
variable the dashboard correlates against each species — precomputed here so the
static frontend needs no SST field of its own.
"""

import json
import math
from datetime import date
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

GROUPS = ["cetacean", "shark", "seal", "fish", "seabird", "other"]

# Cap the cells STORED per species for the map (top-K by count) to bound file
# size and keep the map legible. The per-species SST series is still computed
# over the species' FULL footprint, so the thermal signal is unaffected.
MAX_MAP_CELLS = 800


# ---------------------------------------------------------------------------
# SST grid helpers
# ---------------------------------------------------------------------------

class SstGrid:
    """Nearest-cell sampler over the global monthly OISST field."""

    def __init__(self, npz_path: Path):
        z = np.load(npz_path, allow_pickle=True)
        self.months: list[str] = [str(m) for m in z["months"]]
        lats = z["lats"].astype("float64")
        lons = z["lons"].astype("float64")
        sst = z["sst"].astype("float32")  # (T, Y, X)
        # Ensure both axes are ascending so searchsorted works; reorder sst too.
        lat_order = np.argsort(lats)
        lon_order = np.argsort(lons)
        self.lats = lats[lat_order]
        self.lons = lons[lon_order]
        self.sst = sst[:, lat_order, :][:, :, lon_order]
        self.n_months = len(self.months)
        # All-time per-cell mean (Y, X), ignoring NaN months (land = all-NaN).
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            self.cell_mean = np.nanmean(self.sst, axis=0)

    def _nearest(self, axis: np.ndarray, vals: np.ndarray) -> np.ndarray:
        idx = np.searchsorted(axis, vals)
        idx = np.clip(idx, 1, len(axis) - 1)
        left = axis[idx - 1]
        right = axis[idx]
        idx = np.where(np.abs(vals - left) <= np.abs(vals - right), idx - 1, idx)
        return idx

    def cell_indices(self, lngs: np.ndarray, lats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self._nearest(self.lats, lats), self._nearest(self.lons, lngs)

    def series_over_cells(self, yi: np.ndarray, xi: np.ndarray, w: np.ndarray):
        """Count-weighted monthly SST over the given cells → (series[T], all-time stats)."""
        col = self.sst[:, yi, xi]            # (T, ncells)
        valid = np.isfinite(col)
        wgt = np.where(valid, w[None, :], 0.0)
        num = np.nansum(np.where(valid, col * w[None, :], 0.0), axis=1)
        den = wgt.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            series = np.where(den > 0, num / den, np.nan)   # (T,)
        # Thermal niche from the per-cell all-time means, weighted by count.
        cm = self.cell_mean[yi, xi]
        finite = np.isfinite(cm)
        if finite.any():
            vals = cm[finite]
            ww = w[finite].astype("float64")
            mean = float(np.average(vals, weights=ww))
            p10 = _weighted_quantile(vals, ww, 0.10)
            p90 = _weighted_quantile(vals, ww, 0.90)
        else:
            mean = p10 = p90 = None
        sm = series[np.isfinite(series)]
        amp = float(sm.max() - sm.min()) if sm.size else None
        return series, mean, p10, p90, amp


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v = values[order]
    w = weights[order]
    cw = np.cumsum(w) - 0.5 * w
    cw /= w.sum()
    return float(np.interp(q, cw, v))


# ---------------------------------------------------------------------------
# Cell universe (dedup centroids → dense index)
# ---------------------------------------------------------------------------

class CellIndex:
    def __init__(self):
        self._id: dict[tuple[float, float], int] = {}
        self.centroids: list[list[float]] = []

    def get(self, lng: float, lat: float) -> int:
        key = (round(lng, 4), round(lat, 4))
        cid = self._id.get(key)
        if cid is None:
            cid = len(self.centroids)
            self._id[key] = cid
            self.centroids.append([key[0], key[1]])
        return cid


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _round(x, n=1):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    obis_path = DATA_DIR / "obis_species.json"
    if not obis_path.exists():
        raise SystemExit("data/obis_species.json not found — run fetch_obis.py first")
    grid_path = DATA_DIR / "oisst_grid.npz"
    if not grid_path.exists():
        raise SystemExit("data/oisst_grid.npz not found — run fetch_oisst.py first")

    print("Loading inputs...")
    obis = json.loads(obis_path.read_text(encoding="utf-8"))
    grid = SstGrid(grid_path)
    months = grid.months
    print(f"  {len(obis['species'])} species, {len(months)} SST months")

    years = list(range(obis.get("start_year", 2015), date.today().year + 1))
    cells = CellIndex()

    # --- species.json --------------------------------------------------------
    species_out = []
    for sp in obis["species"]:
        raw_cells = sp.get("cells", [])
        if not raw_cells:
            continue
        lngs = np.array([c[0] for c in raw_cells], dtype="float64")
        lats = np.array([c[1] for c in raw_cells], dtype="float64")
        counts = np.array([c[2] for c in raw_cells], dtype="float64")

        yi, xi = grid.cell_indices(lngs, lats)
        # SST series/niche use the FULL footprint (all cells).
        series, sst_mean, sst_p10, sst_p90, amp = grid.series_over_cells(yi, xi, counts)

        # Store only the top-K cells by count for the map (bounds size + clutter).
        top = np.argsort(counts)[::-1][:MAX_MAP_CELLS]
        cell_refs = [[cells.get(float(lngs[k]), float(lats[k])), int(counts[k])]
                     for k in top]

        yc = {y: sp.get("year_counts", {}).get(str(y), 0) for y in years}
        present_years = [y for y in years if yc[y] > 0]

        species_out.append({
            "id": len(species_out),
            "aphia_id": sp.get("aphia_id"),
            "scientific_name": sp.get("scientific_name"),
            "common_name": None,   # enriched client-side (Wikipedia)
            "group": sp.get("group"),
            "taxonomy": sp.get("taxonomy"),
            "sighting_count": int(sp.get("records_total", 0)),
            "cell_count": len(raw_cells),
            "first_year": present_years[0] if present_years else None,
            "last_year": present_years[-1] if present_years else None,
            "sst": {"mean": _round(sst_mean), "p10": _round(sst_p10),
                    "p90": _round(sst_p90), "amp": _round(amp)},
            "cells": cell_refs,
            "year_counts": {str(y): yc[y] for y in years if yc[y] > 0},
            "series_sst": [_round(v) for v in series],
        })

    species_out.sort(key=lambda s: s["sighting_count"], reverse=True)
    for i, s in enumerate(species_out):   # reassign ids after sort
        s["id"] = i

    # --- density.json --------------------------------------------------------
    density_out = []
    for lng, lat, cnt in obis.get("density", []):
        cid = cells.get(lng, lat)
        yi, xi = grid.cell_indices(np.array([lng]), np.array([lat]))
        sm = float(grid.cell_mean[yi[0], xi[0]])
        density_out.append([cid, int(cnt), _round(sm)])

    # --- cells.json ----------------------------------------------------------
    cells_out = {
        "grid_precision": obis.get("grid_precision", 3),
        "cells": cells.centroids,   # index == cell id, [lng, lat]
    }

    species_doc = {
        "generated": obis.get("generated", date.today().strftime("%Y-%m-%d")),
        "months": months,
        "years": years,
        "groups": GROUPS,
        "sources": {
            "sightings": "OBIS (Ocean Biodiversity Information System) aggregation API",
            "sst": "NOAA OISST v2.1 monthly (NOAA PSL)",
        },
        "species": species_out,
    }

    _write("cells.json", cells_out)
    _write("species.json", species_doc)
    _write("density.json", {"cells": density_out})

    print(f"\n  {len(species_out)} species, {len(cells.centroids)} unique cells, "
          f"{len(density_out)} density cells")


def _write(filename: str, data) -> None:
    def sanitize(o):
        if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
            return None
        if isinstance(o, dict):
            return {k: sanitize(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sanitize(v) for v in o]
        return o

    path = OUTPUT_DIR / filename
    path.write_text(json.dumps(sanitize(data), ensure_ascii=False), encoding="utf-8")
    size_mb = path.stat().st_size / (1024 * 1024)
    flag = "  ⚠ >5MB (→ public/data)" if size_mb > 5 else ""
    print(f"  {filename:<16} {size_mb:>6.2f} MB{flag}")


if __name__ == "__main__":
    main()
