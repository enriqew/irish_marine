#!/usr/bin/env python3
"""
pipeline.py — Run the full Marine Atlas data pipeline end to end.

Execution order:
  1. fetch_obis.py            — curated global species LIST + density backdrop
                                from OBIS aggregation endpoints
  2. build_footprints_raw.py  — per-species footprint (all-time + MONTHLY) +
                                annual counts from the raw OBIS dump (0.1° grid);
                                one local scan, replaces the grid + monthly APIs
  3. fetch_oisst.py           — download the global ~1° monthly NOAA OISST field
  4. process.py               — join SST to each species' cells and emit
                                output/*.json (incl. output/monthly/*.json)

Step 2 needs the ~91 GB raw OBIS dump under G:\obis_raw (one-time aws s3 sync);
it builds a fast subset on first run. Fetch failures are reported but non-fatal;
process.py errors out only if a required input is missing.

SST RASTER TILES (separate): scripts/render_sst_tiles.py turns the OISST grid
into colour PNGs (land transparent) → output/sst/<YYYY-MM>.png + mean.png +
field.json, the temperature *surface* the frontend shows as an ImageOverlay.
Run after fetch_oisst.py; copy output/sst/ into public/data/marine-atlas/sst/.
"""

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fetch_obis
import build_footprints_raw
import fetch_oisst
import process


def _run_step(name: str, fn) -> bool:
    print(f"\n{'=' * 60}\n  {name}\n{'=' * 60}")
    t0 = time.perf_counter()
    try:
        fn()
        print(f"\n  [OK]   {name} in {time.perf_counter() - t0:.1f}s")
        return True
    except Exception:
        print(f"\n  [FAIL] {name} after {time.perf_counter() - t0:.1f}s:")
        traceback.print_exc()
        return False


def main() -> None:
    print("=" * 60)
    print("  Marine Atlas — Data Pipeline")
    print("=" * 60)
    t_start = time.perf_counter()

    results = {
        "OBIS snapshot": _run_step("OBIS snapshot", fetch_obis.main),
    }
    if results["OBIS snapshot"]:
        results["Raw footprints"] = _run_step(
            "Raw footprints (all-time + monthly)", build_footprints_raw.main)
    results["OISST field"] = _run_step("OISST field", fetch_oisst.main)
    results["Process"] = _run_step("Process -> output/", process.main)
    if not results["Process"]:
        print("\nPipeline aborted: processing step failed.")
        sys.exit(1)

    print(f"\n{'=' * 60}\n  Finished in {time.perf_counter() - t_start:.1f}s\n{'=' * 60}")
    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"  Steps with errors: {', '.join(failed)}")
    for f in sorted(process.OUTPUT_DIR.glob("*.json")):
        print(f"    {f.name:<20} {f.stat().st_size / 1024:>8.1f} KB")


if __name__ == "__main__":
    main()
