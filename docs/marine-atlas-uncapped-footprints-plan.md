# Uncapped footprints — show every cell, every species

**Date: 2026-08-21.** Trigger: *Larus fuscus* looks absent from Ireland on the map even
though it is abundant there. Two stacked causes: (1) OBIS coverage bias — 40 % of the
species' 3.9 M world records sit in one Belgian/Dutch bbox (LifeWatch GPS gulls), Ireland
has only 9 k; (2) **the `MAX_MAP_CELLS = 1500` top-by-count cap** then drops nearly all
Irish cells (cut-off was 197 records/cell). 273 of 636 species exceed the cap; in total
the maps show ~0.9 M of 2.63 M cell entries. Decision (Enrique): **remove the caps —
ship every cell for every species.**

## Why it is cheap

Both caps live only in `process.py` — `build_footprints_raw.py`'s intermediates are
already uncapped (`data/obis_species.json` 48 MB all-time; `data/obis_monthly/` 11 MB,
largest checkpoint 1 MB) and the SST niche is already computed over the FULL footprint.
So: **no DuckDB re-aggregation, no re-fetch** — edit `process.py`, re-run it, copy
artifacts.

## Changes

### Pipeline (`process.py`)

1. **Drop `MAX_MAP_CELLS`.** Per-species footprint files become **self-contained**
   (same pattern the monthly files already use): `{"id": N, "cells": [[lng, lat,
   count], ...]}` sorted by count desc (draw order: big first, small on top). They no
   longer reference `cells.json` by index — at the full universe the shared centroid
   table would balloon (~2 M distinct cells), and self-contained files decouple it.
2. **Drop `MAX_MONTHLY_CELLS`.** Monthly frames keep their existing self-contained
   format, just uncapped (total ≈ 11 MB, fine).
3. **`cells.json` shrinks to density-only** — `CellIndex` is now fed only by the
   density backdrop loop. Keep format unchanged (frontend density code untouched).
4. **New meta field `cell_universe`** in `species.json` (distinct 0.1° cells across all
   species footprints) — replaces the sidebar "Cells" stat that used to read
   `CELL_CENTROIDS.length`.

### Frontend (portfolio repo)

- `data.ts`: `SpeciesFootprint.cells` → `[lng, lat, count][]`; export `CELL_UNIVERSE`.
- `MarineAtlasMap.tsx`: two spots stop cross-referencing `CELL_CENTROIDS`
  (speciesPts memo + fitBounds loop) — footprint cells now carry their own coords.
- `MarineAtlasSidebar.tsx`: "Cells" stat reads `CELL_UNIVERSE`.
- Schema doc (`docs/marine-atlas-data-schema.md`) updated.

## Expected sizes

- `footprints/` total ≈ 50 MB in `public/` (largest species, 206,623 cells ≈ 4–5 MB
  raw, ~1 MB gzipped over CloudFront; lazy-loaded on select, spinner already exists).
- `monthly/` ≈ 11 MB total.
- `species.json` unchanged (~0.8 MB); `cells.json` shrinks from 2.87 MB to ~kB.
- MapLibre renders 200 k-point GeoJSON circle layers fine; verify with Playwright.

## Verify

- `pytest` (pipeline), `npm run test` + `npm run build` (portfolio).
- Playwright on the dev server: select *Larus fuscus* → Irish cells now visible.
- Spot-check a small species (footprint unchanged) and the largest (renders, flies).
