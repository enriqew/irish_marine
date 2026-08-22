# Marine Atlas — data pipeline

A Python pipeline that builds a **global, species-centric** marine-biodiversity
dataset from open data and writes small, static JSON artefacts that power an
interactive MapLibre GL dashboard. It queries OBIS's **server-side aggregation
endpoints** to assemble a curated species list, computes each species' map
**footprint from the raw OBIS occurrence dump** (aggregated locally to a fine 0.1°
grid), then joins each species to the NOAA OISST satellite sea-surface-temperature
field.

This repository is **data + processing only** — no UI. The dashboard that consumes
these artefacts lives in a separate portfolio project (`data-dive-design-hub`,
component `src/components/MarineAtlas/`). The contract between the two is the schema
of the files in `output/` (documented there in `docs/marine-atlas-data-schema.md`).

> The repository directory is still named `irish_marine` for historical reasons —
> the project began as an Ireland-only dashboard and was re-scoped to the global
> **Marine Atlas** in July 2026.

> **No mock data.** Every value comes from a real source. If a source is
> unavailable it is dropped rather than faked.

---

## Two data paths: aggregation API for the list, raw dump for footprints

**The species list + density come from OBIS's aggregation API** (a few cheap requests,
no bulk download):

- `/checklist?scientificname=<taxon>` — species under a taxon, each with an all-time
  `records` count and full WoRMS taxonomy → used to assemble a **curated** species
  list (top-N by records per group; see `CURATED_TAXA` in `fetch_obis.py`).
- `/occurrence/grid/2` (all species) — one coarse global density backdrop.

**The per-species footprint + monthly frames + annual counts come from the raw OBIS
occurrence dump.** The grid API only offers ~1.4° cells (`/occurrence/grid/3`), which
render as blocky squares, and a monthly footprint there would need ~60 k requests. So
`build_footprints_raw.py` instead reads the **raw occurrences** — OBIS's full GeoParquet
dump on AWS Open Data (`s3://obis-open-data/occurrence`, ~201.5 M records / ~91 GB,
synced locally once) — and aggregates the actual coordinates to a fine **0.1° grid**
(~11 km) with DuckDB. One local scan yields the all-time footprint, the per-month
animation frames, and the annual counts, all at once — point-like footprints from real
density instead of API-side ~1.4° blocks.

The heavy raw scan is a one-off: it builds a flat, aphiaid-sorted **fast subset**
(`marine_atlas_subset.parquet`, ~1-3 GB) of just the curated species, and all later
runs aggregate from that in seconds.

---

## Architecture

```
        SOURCES                        FETCH / BUILD                INTERMEDIATE (data/, git-ignored)
 ┌──────────────────────────────┐   ┌──────────────────────┐   ┌──────────────────────────┐
 │ OBIS aggregation API         │─▶ │ fetch_obis.py        │─▶ │ obis_species.json        │
 │  /checklist  /grid/2         │   │  (curated list +     │   │  (list + records + density)│
 │                              │   │   density backdrop)  │   └───────────┬──────────────┘
 │ OBIS raw dump (GeoParquet,   │─▶ │ build_footprints_raw │──────────────▶│ (adds cells +
 │  AWS Open Data ~91 GB, G:)   │   │  .py (DuckDB, 0.1°)  │─▶ obis_monthly/│  year_counts +
 │                              │   │                      │   <aphiaid>.json  monthly frames)
 │ NOAA OISST v2.1 (AWS Open    │─▶ │ fetch_oisst.py       │─▶ │ oisst_grid.npz           │
 │  Data, daily, global 0.25°)  │   │  (monthly means, 0.25°)│ │  (months, lats, lons, sst)│
 └──────────────────────────────┘   └──────────────────────┘   └───────────┬──────────────┘
                                                                            │
                                        PROCESS (numpy)                     ▼
                                        ┌──────────────────────────────────────────────┐
                                        │ process.py                                   │
                                        │  • dedup 0.1° cells → dense cell_id index    │
                                        │  • join OISST to each species' cells          │
                                        │  • per-species monthly SST series (weighted)  │
                                        │  • thermal niche (mean / p10 / p90 / amp)     │
                                        │  • per-species monthly footprint files        │
                                        └───────────────────────┬──────────────────────┘
                                                                ▼
                                  OUTPUT ARTEFACTS (output/)
                    cells.json · species.json · density.json (committed)
                    footprints/*.json · monthly/*.json · sst/*.png (git-ignored, shipped)
                                                                │
                                                                ▼
                               consumed by the MapLibre GL dashboard (separate repo)
```

`pipeline.py` runs `fetch_obis` → `build_footprints_raw` → `fetch_oisst` → `process`.
Fetch failures are reported but non-fatal; `process.py` errors out only if a required
input is missing. `build_footprints_raw` needs the local raw dump (see below).

---

## Data sources

| Source | Provides | Auth |
|---|---|---|
| [OBIS](https://api.obis.org) aggregation API | Curated species list + taxonomy + record totals, global density backdrop | none |
| [OBIS raw dump](https://obis.org/data/access/) (AWS Open Data GeoParquet, `s3://obis-open-data/occurrence`) | Raw occurrences → per-species 0.1° footprint, monthly frames, annual counts | none (anonymous S3) |
| [NOAA OISST v2.1](https://www.ncei.noaa.gov/products/optimum-interpolation-sst) (daily files on AWS Open Data, `s3://noaa-cdr-sea-surface-temp-optimum-interpolation-pds`) | Gap-free satellite SST, global 0.25°; monthly means built locally | none (anonymous S3) |

Licensing and attribution: see [`DATA_LICENSE.md`](DATA_LICENSE.md).

---

## Output artefacts

The three core JSONs are comfortably under 5 MB so the dashboard bundles them
directly; the per-species files and the SST tiles are served lazily from
`public/`.

| File | Size | Description |
|---|---|---|
| `cells.json` | ~0.02 MB | 0.1° cell centroids `[[lng, lat], …]` for the **density backdrop only** — `density.json` references the array index as `cell_id`. (Footprint files carry their own coordinates; see below.) |
| `species.json` | ~0.8 MB | Catalog of ~636 curated species: taxonomy, annual `year_counts`, monthly `series_sst`, thermal niche `{mean, p10, p90, amp}`, `cell_count`, `has_monthly`. Plus shared `months[]` / `years[]` axes and `cell_universe` (the distinct-cell total across all species). **Footprint cells are NOT here** (see below). |
| `footprints/<id>.json` | ~48 MB total | Per-species all-time footprint, **self-contained and uncapped** — every 0.1° cell as `[lng, lat, count]`, sorted by count descending so the map draws dense cells first and sparse ones stay on top. Largest is the southern elephant seal at ~3.7 MB / 206 k cells. Lazy-loaded when a species is selected. |
| `density.json` | ~0.02 MB | Global all-species grid `[cell_id, total_count, sst_mean]` — the idle backdrop. |
| `monthly/<id>.json` | ~11 MB total | Per-species monthly footprint frames (`{month: [[lng, lat, count], …]}`, self-contained, uncapped), lazy-loaded by the dashboard for the map's month-by-month animation. |
| `sst/<YYYY-MM>.png` + `sst/mean.png` | ~0.5-1 MB each | The OISST field rendered as colour PNG tiles (land transparent, Web-Mercator-aligned) — the temperature *surface* the map drapes on the globe, swapped per month as the timeline plays. From `render_sst_tiles.py`. |
| `sst/field.json` | tiny | Tile metadata: months, overlay bounds, temperature range. Shipped bundled as `sst-field.json`. |

The heart of the dataset is `species.json`'s **`series_sst`**: for each species, the
count-weighted monthly mean SST **over the cells where it occurs**. A tropical species
yields a warm seasonal curve, a polar one a cold curve — the temperature-as-temporal
signal the dashboard correlates per species. Full schema:
`data-dive-design-hub/docs/marine-atlas-data-schema.md`.

### Curated species (taxonomic groups)

`CURATED_TAXA` in `fetch_obis.py` maps ~25 higher taxa to the six display groups
(`cetacean | shark | seal | fish | seabird | other`) with a per-taxon top-N by record
count. Edit that table to change the snapshot; the whole pipeline is driven by it.

---

## Getting started

Requires Python 3.12+.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -r requirements.txt
```

### One-time: sync the raw OBIS dump

`build_footprints_raw.py` needs OBIS's raw occurrence dump locally (~91 GB). Sync it
once from AWS Open Data (anonymous, no AWS account):

```bash
aws s3 sync --no-sign-request --region us-east-1 \
    s3://obis-open-data/occurrence/ G:/obis_raw/occurrence/
```

Point elsewhere with the `OBIS_RAW_ROOT` env var (default `G:/obis_raw`). The first
pipeline run builds a ~1-3 GB fast subset (`marine_atlas_subset.parquet`) from it; every
later run reuses that and finishes in minutes.

### Run the pipeline

```bash
python scripts/pipeline.py          # fetch_obis → build_footprints_raw → fetch_oisst → process
```

Or run steps individually:

```bash
python scripts/fetch_obis.py            # curated species list + density → data/obis_species.json
python scripts/build_footprints_raw.py  # 0.1° footprint + monthly + annual counts (raw dump, DuckDB)
python scripts/fetch_oisst.py           # global 0.25° monthly-mean SST → data/oisst_grid.npz
python scripts/process.py               # join + emit → output/{cells,species,density}.json + monthly/
python scripts/render_sst_tiles.py      # OISST → output/sst/*.png + field.json (not part of pipeline.py — run after fetch_oisst)
```

Intermediates land in `data/` (git-ignored). Of the artefacts in `output/`, only the
three core JSONs are committed here; the per-species and raster files (`footprints/`,
`monthly/`, `sst/` — ~177 MB, fully regenerable) are git-ignored and live versioned in
the portfolio repo instead. To ship a refresh:
`output/{cells,species,density}.json` → portfolio `src/data/marine-atlas/` (bundled),
`output/sst/field.json` → `src/data/marine-atlas/sst-field.json` (bundled), and
`output/footprints/`, `output/monthly/`, `output/sst/*.png` →
`public/data/marine-atlas/` (fetched lazily at runtime).

---

## Notable implementation details

- **OBIS aggregation (list)** — `fetch_obis.py` reads only summaries with 429 back-off
  and timeout retries. Curated list built from `/checklist` (accepted, marine/brackish
  species only), sorted by record count; one `/occurrence/grid/2` call for the density
  backdrop.
- **Raw footprints (DuckDB)** — `build_footprints_raw.py` aggregates the raw 91 GB
  GeoParquet dump to a 0.1° grid with three GROUP BYs over a sorted fast subset. Points
  on land / in brackish water are **kept on purpose** (honest data — e.g. anadromous
  salmonids legitimately sit inland; their SST niche is sampled at the nearest ocean
  cell). No `shoredistance` filter.
- **OISST from AWS, not OPeNDAP** — NOAA PSL's THREDDS server (the pre-aggregated
  monthly product) rate-limits hard (HTTP 429), so `fetch_oisst.py` builds the monthly
  means itself from OISST's **daily** files mirrored on AWS Open Data (anonymous S3,
  no limits), sampling every 3rd day per month with 16 parallel downloads — the
  standard error of a ~10-day mean is well under 0.1 °C for a colour field. Native
  0.25° is kept; longitudes are converted 0-360 → −180/180 and land (NaN) preserved.
- **SST raster tiles** — `render_sst_tiles.py` reprojects each monthly field to Web
  Mercator (so it aligns pixel-for-pixel with the basemap) with bilinear resampling,
  and antialiases the coastline by thresholding a 4×-supersampled ocean mask into a
  continuous alpha channel. PNGs are written by a small pure numpy+zlib encoder — no
  imaging dependency.
- **SST join** — `process.py` samples the OISST grid at each species' cell centroids
  (nearest cell) and computes a count-weighted monthly mean and an all-time thermal
  niche (weighted mean + p10/p90 quantiles). Footprints ship **uncapped** — every cell
  of every species — and the SST series uses the same full footprint. (An earlier
  top-1500-cells-per-species cap hid genuinely occupied but under-sampled regions, e.g.
  Ireland for the lesser black-backed gull, whose records are dominated by one Belgian
  GPS-tracking dataset; removed 2026-08-21, see `docs/marine-atlas-uncapped-footprints-plan.md`.)
- **NaN/Inf sanitisation** — all outputs are sanitised so the JSON is always valid.

---

## License

Source code: MIT (see [`LICENSE`](LICENSE)). Data artefacts are derived from
third-party sources under their own terms — see [`DATA_LICENSE.md`](DATA_LICENSE.md).
