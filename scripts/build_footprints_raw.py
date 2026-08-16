#!/usr/bin/env python3
"""
build_footprints_raw.py — Per-species footprints (all-time + monthly) and annual
counts from the RAW OBIS occurrence dump, replacing the OBIS grid-API aggregation.

Why raw
-------
The grid API (``/occurrence/grid/3``) returns ~1.4° cells → blocky footprints. The
full OBIS GeoParquet dump (AWS Open Data ``s3://obis-open-data/occurrence``, ~91 GB,
synced to ``G:\\obis_raw\\occurrence``) holds the actual occurrence coordinates, so we
aggregate them to a fine ``CELL_DEG`` (0.1° ≈ 11 km) grid: point-like footprints from
real density, and ONE local scan yields the all-time footprint, the per-month frames,
AND the annual counts — replacing both the grid fetch and the ~60k-request monthly API
crawl that ``fetch_obis_monthly.py`` used to do.

Flow
----
  1. Read the curated aphiaids from ``data/obis_species.json`` (from fetch_obis.py).
  2. ONE DuckDB ``GROUP BY`` over the full dump (reading only the 5 needed columns,
     filtered to those species) aggregates occurrences to ``(aphiaid, 0.1° cell,
     year, month)`` counts — a parallel hash aggregate, no sort, no intermediate.
  3. Split that in pandas into all-time footprint / annual counts / monthly cells,
     then roll each up per species.
  4. Write ``cells`` + ``year_counts`` back into ``obis_species.json`` and emit
     ``data/obis_monthly/<aphiaid>.json`` in the exact format ``process.py``'s
     ``build_monthly()`` already consumes — so process.py and the frontend are
     unchanged; only the *source* of the footprints changed.

Reading OBIS's nested ``interpreted`` struct is CPU-bound and DuckDB under-parallelises
it (observed: ~3 of 16 cores, disk idle, RAM free — a single ``GROUP BY`` left the
machine mostly idle for ~1 h). So we **shard the 7 109 parquet files across a process
pool** (one single-threaded DuckDB per worker) to saturate every core, then merge the
compact partial aggregates. We also deliberately do NOT build a sorted subset first: a
``COPY ... ORDER BY`` of the ~45 M matching rows buffers them single-threaded and is
pathologically slow (observed: >3 h, memory climbing to 13 GB).

Requires ``duckdb`` (in .venv) and the 91 GB local dump. There is no API fallback:
the grid-API footprint path was retired when this script landed. Point the dump
elsewhere with ``OBIS_RAW_ROOT``.
"""

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import duckdb
import pandas as pd

import fetch_obis
from fetch_obis import START_YEAR

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MONTHLY_DIR = DATA_DIR / "obis_monthly"

# Raw dump location (AWS Open Data s3://obis-open-data/occurrence synced locally).
OBIS_RAW_ROOT = Path(os.environ.get("OBIS_RAW_ROOT", "G:/obis_raw"))
OCC_DIR = OBIS_RAW_ROOT / "occurrence"

CELL_DEG = 0.1          # footprint grid cell size → round coords to 1 decimal
GRID_LABEL = "0.1deg"   # written to artifacts as the footprint resolution tag
N_WORKERS = min(16, (os.cpu_count() or 8))   # parallel DuckDB processes


# ---------------------------------------------------------------------------
# Parallel aggregation — shard the parquet files across a process pool
# ---------------------------------------------------------------------------

def _agg_shard(payload: tuple[list[str], str]) -> pd.DataFrame:
    """Worker: aggregate one shard of parquet files to (aphiaid, 0.1° cell, year,
    month, count). Single-threaded DuckDB — parallelism comes from many workers."""
    files, id_list = payload
    con = duckdb.connect()
    con.execute("PRAGMA threads=1")
    con.execute("PRAGMA memory_limit='3GB'")
    files_sql = "[" + ",".join(f"'{f}'" for f in files) + "]"
    return con.execute(f"""
        SELECT interpreted.aphiaid                    AS aphiaid,
               round(interpreted.decimalLongitude, 1) AS glng,
               round(interpreted.decimalLatitude, 1)  AS glat,
               TRY_CAST(interpreted.year AS INTEGER)  AS year,
               TRY_CAST(interpreted.month AS INTEGER) AS m,
               count(*)                                AS n
        FROM read_parquet({files_sql})
        WHERE dropped IS NOT TRUE
          AND interpreted.aphiaid IN ({id_list})
          AND interpreted.decimalLatitude IS NOT NULL
          AND interpreted.decimalLongitude IS NOT NULL
        GROUP BY 1, 2, 3, 4, 5
    """).df()


def _aggregate(id_list: str):
    """Shard the dump across N_WORKERS processes, aggregate each in parallel, merge
    the partials, and split into the three per-species frames. On-land / brackish
    points are kept on purpose (no shoredistance filter) — see the data schema doc."""
    files = sorted(str(p.as_posix()) for p in OCC_DIR.glob("*.parquet"))
    if not files:
        raise SystemExit(
            f"raw OBIS dump not found under {OCC_DIR} — sync it once with:\n"
            "  aws s3 sync --no-sign-request --region us-east-1 "
            f"s3://obis-open-data/occurrence/ {OBIS_RAW_ROOT.as_posix()}/occurrence/")

    shards = [files[i::N_WORKERS] for i in range(N_WORKERS)]   # round-robin split
    print(f"  {len(files)} parquet files across {N_WORKERS} workers "
          f"(~{len(shards[0])} files each)...", flush=True)

    parts: list[pd.DataFrame] = []
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=N_WORKERS) as pool:
        futs = {pool.submit(_agg_shard, (s, id_list)): k
                for k, s in enumerate(shards)}
        for done, fut in enumerate(as_completed(futs), 1):
            part = fut.result()
            parts.append(part)
            print(f"    shard {done}/{N_WORKERS} done · {len(part):,} rows · "
                  f"{time.perf_counter() - t0:.0f}s elapsed", flush=True)

    # Merge partials: the same (aphiaid, cell, year, month) can appear in several
    # shards, so re-aggregate their counts.
    combined = (pd.concat(parts, ignore_index=True)
                .groupby(["aphiaid", "glng", "glat", "year", "m"], as_index=False,
                         dropna=False)["n"].sum())
    return _split(combined)


def _split(combined):
    """Aggregated (aphiaid, glng, glat, year, m, n) rows → the three frames the
    rollup consumes: all-time footprint / annual counts / monthly cells."""
    # All-time footprint: sum over year+month → the species' full range.
    foot = (combined.groupby(["aphiaid", "glng", "glat"], as_index=False)["n"]
            .sum())

    # >= START_YEAR slice (NaN years compare False and drop out).
    y15 = combined[combined["year"] >= START_YEAR]

    # Annual counts: month-agnostic (includes month-less records).
    yr = y15.groupby(["aphiaid", "year"], as_index=False)["n"].sum()

    # Monthly cells: only rows with a real month; already one row per cell-month.
    mon = y15[y15["m"].between(1, 12)][
        ["aphiaid", "glng", "glat", "year", "m", "n"]].copy()
    return foot, yr, mon


# ---------------------------------------------------------------------------
# Roll DuckDB results up into the per-species shapes process.py expects
# ---------------------------------------------------------------------------

def rollup(foot, yr, mon) -> tuple[dict, dict, dict]:
    """(footprints, year_counts, monthly) keyed by aphiaid.

    footprints : {aid: [[lng, lat, count], ...]}          (all-time)
    year_counts: {aid: {"YYYY": count, ...}}              (>= START_YEAR)
    monthly    : {aid: {"YYYY-MM": [[lng, lat, count], ...]}}
    """
    footprints: dict[int, list] = {}
    for aid, g in foot.groupby("aphiaid"):
        footprints[int(aid)] = [[round(float(l), 1), round(float(a), 1), int(n)]
                                for l, a, n in zip(g.glng, g.glat, g.n)]

    year_counts: dict[int, dict] = {}
    for aid, g in yr.groupby("aphiaid"):
        year_counts[int(aid)] = {str(int(y)): int(n) for y, n in zip(g.year, g.n)}

    monthly: dict[int, dict] = {}
    for aid, g in mon.groupby("aphiaid"):
        frames: dict[str, list] = {}
        for l, a, y, m, n in zip(g.glng, g.glat, g.year, g.m, g.n):
            frames.setdefault(f"{int(y)}-{int(m):02d}", []).append(
                [round(float(l), 1), round(float(a), 1), int(n)])
        monthly[int(aid)] = frames

    return footprints, year_counts, monthly


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    obis_path = DATA_DIR / "obis_species.json"
    if not obis_path.exists():
        raise SystemExit("data/obis_species.json not found — run fetch_obis.py first")

    obis = json.loads(obis_path.read_text(encoding="utf-8"))
    species = obis["species"]
    ids = [s["aphia_id"] for s in species if s.get("aphia_id") is not None]
    id_list = ",".join(str(i) for i in ids)
    print(f"Building raw footprints for {len(ids)} species (cell {CELL_DEG}°)...",
          flush=True)

    print("  aggregating the raw dump (sharded across processes)...", flush=True)
    t0 = time.perf_counter()
    foot, yr, mon = _aggregate(id_list)
    print(f"  aggregated in {time.perf_counter() - t0:.0f}s "
          f"({len(foot):,} footprint rows, {len(mon):,} monthly cell-rows)", flush=True)
    footprints, year_counts, monthly = rollup(foot, yr, mon)

    # --- write cells + year_counts back into obis_species.json ---------------
    n_no_cells = 0
    for sp in species:
        aid = sp.get("aphia_id")
        sp["cells"] = footprints.get(aid, [])
        sp["year_counts"] = year_counts.get(aid, {})
        if not sp["cells"]:
            n_no_cells += 1
    obis["grid_precision"] = GRID_LABEL
    obis["footprint_source"] = (
        "OBIS raw occurrences (AWS Open Data GeoParquet), 0.1° grid")
    obis_path.write_text(json.dumps(obis, ensure_ascii=False), encoding="utf-8")

    # --- emit per-species monthly checkpoints (process.py build_monthly fmt) --
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    for stale in MONTHLY_DIR.glob("*.json"):   # drop the old precision-3 API frames
        stale.unlink()
    by_aid = {s["aphia_id"]: s for s in species}
    for aid, frames in monthly.items():
        sp = by_aid.get(aid)
        (MONTHLY_DIR / f"{aid}.json").write_text(json.dumps({
            "taxon_id": aid,
            "aphia_id": aid,
            "scientific_name": sp.get("scientific_name") if sp else None,
            "group": sp.get("group") if sp else None,
            "start_year": START_YEAR,
            "grid_precision": GRID_LABEL,
            "complete": True,
            "frames": frames,
        }, ensure_ascii=False), encoding="utf-8")

    total_cells = sum(len(v) for v in footprints.values())
    print(f"\n  {len(footprints)} species with footprints "
          f"({n_no_cells} with none), {total_cells} all-time cells total")
    print(f"  {len(monthly)} species with monthly frames -> {MONTHLY_DIR}")
    print(f"  cells + year_counts written back to {obis_path.name}")


if __name__ == "__main__":
    main()
