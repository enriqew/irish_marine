# Marine Atlas — data pipeline

A Python pipeline that builds a **global, species-centric** marine-biodiversity
dataset from open data and writes small, static JSON artefacts that power an
interactive Leaflet dashboard. It does **not** download raw occurrence records —
OBIS holds ~201 million of them (~400 GB) — but queries OBIS's **server-side
aggregation endpoints** for a curated set of species, then joins each species to
the NOAA OISST satellite sea-surface-temperature field.

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

## Why aggregation, not a bulk download

A global raw OBIS pull is ~201.5 M records / ~400 GB — pointless to download for a
static dashboard. OBIS already aggregates server-side, so the pipeline asks OBIS for
summaries instead of occurrences:

- `/checklist?scientificname=<taxon>` — species under a taxon, each with an all-time
  `records` count and full WoRMS taxonomy → used to assemble a **curated** species
  list (top-N by records per group; see `CURATED_TAXA` in `fetch_obis.py`).
- `/occurrence/grid/{precision}?taxonid=<id>` — GeoJSON grid of per-cell counts for
  a species (precision 3 ≈ 1.4° cells) → the species' map footprint.
- `/statistics/years?taxonid=<id>` — annual occurrence counts for a species.
- `/occurrence/grid/2` (all species) — one coarse global density backdrop.

Result: ~2 requests per species (~1.2 k total for the curated set), runs locally in
minutes, no bulk storage, no AWS.

---

## Architecture

```
        PUBLIC SOURCES                    FETCH (httpx / xarray)      INTERMEDIATE (data/, git-ignored)
 ┌──────────────────────────────┐     ┌─────────────────────┐     ┌──────────────────────────┐
 │ OBIS aggregation API         │──▶  │ fetch_obis.py       │──▶  │ obis_species.json        │
 │  /checklist /grid /statistics│     │  (curated list +    │     │  (list + footprint +     │
 │                              │     │   per-species grid) │     │   annual counts)         │
 │ NOAA OISST v2.1 (PSL OPeNDAP)│──▶  │ fetch_oisst.py      │──▶  │ oisst_grid.npz           │
 │  monthly, global 0.25°       │     │  (global ~1° stride)│     │  (months, lats, lons, sst)│
 └──────────────────────────────┘     └─────────────────────┘     └───────────┬──────────────┘
                                                                               │
                                          PROCESS (numpy)                      ▼
                                          ┌──────────────────────────────────────────────┐
                                          │ process.py                                   │
                                          │  • dedup grid cells → dense cell_id index    │
                                          │  • join OISST to each species' cells          │
                                          │  • per-species monthly SST series (weighted)  │
                                          │  • thermal niche (mean / p10 / p90 / amp)     │
                                          └───────────────────────┬──────────────────────┘
                                                                  ▼
                                        OUTPUT ARTEFACTS (output/, committed)
                                        cells.json · species.json · density.json
                                                                  │
                                                                  ▼
                                     consumed by the Leaflet dashboard (separate repo)
```

`pipeline.py` runs the two fetch steps then `process.py`. Fetch failures are reported
but non-fatal; `process.py` errors out only if a required input is missing.

---

## Data sources

| Source | Provides | Auth |
|---|---|---|
| [OBIS](https://api.obis.org) aggregation API | Curated species list, per-species footprint grid, annual counts | none |
| [NOAA OISST v2.1](https://www.ncei.noaa.gov/products/optimum-interpolation-sst) (monthly, via NOAA PSL OPeNDAP) | Gap-free satellite SST, global 0.25° monthly field | none |

Licensing and attribution: see [`DATA_LICENSE.md`](DATA_LICENSE.md).

---

## Output artefacts

Three files, all comfortably under 5 MB so the dashboard bundles them directly.

| File | Size | Description |
|---|---|---|
| `cells.json` | ~0.4 MB | Deduplicated grid-cell centroids `[[lng, lat], …]`; occurrences reference the array index as `cell_id`. |
| `species.json` | ~2.5 MB | Catalog of ~636 curated species: taxonomy, footprint cells `[cell_id, count]` (top-800), annual `year_counts`, monthly `series_sst`, and a thermal niche `{mean, p10, p90, amp}`. Plus shared `months[]` / `years[]` axes. |
| `density.json` | ~0.02 MB | Global all-species grid `[cell_id, total_count, sst_mean]` — the idle backdrop. |

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

### Run the pipeline

```bash
python scripts/pipeline.py          # fetch_obis → fetch_oisst → process
```

Or run steps individually:

```bash
python scripts/fetch_obis.py        # curated species list + footprint + counts → data/obis_species.json
python scripts/fetch_oisst.py       # global ~1° monthly SST → data/oisst_grid.npz
python scripts/process.py           # join + emit → output/{cells,species,density}.json
```

Intermediates land in `data/` (git-ignored). The three artefacts in `output/` are
committed so the dashboard can consume them without re-running the pipeline. To ship
them, copy `output/*.json` into the portfolio's `src/data/marine-atlas/`.

---

## Notable implementation details

- **OBIS aggregation** — the pipeline never downloads occurrences; it reads gridded
  and statistical summaries with 429 back-off and timeout retries. Curated list built
  from `/checklist` (accepted, marine species only), sorted by record count.
- **OISST striding** — the global monthly field is subset by *striding* the 0.25°
  OPeNDAP grid (every 4th cell → ~1°, ~35 MB instead of ~540 MB). **Gotcha:** a single
  strided request for the whole time range returns **all-zeros** from the THREDDS
  server — it must be loaded in month chunks (`TIME_CHUNK`). Longitudes are converted
  0-360 → −180/180 and land (NaN) is preserved.
- **SST join** — `process.py` samples the OISST grid at each species' cell centroids
  (nearest cell) and computes a count-weighted monthly mean and an all-time thermal
  niche (weighted mean + p10/p90 quantiles). Footprint cells are capped at the top 800
  by count for the map, but the SST series uses the species' full footprint.
- **NaN/Inf sanitisation** — all outputs are sanitised so the JSON is always valid.

---

## License

Source code: MIT (see [`LICENSE`](LICENSE)). Data artefacts are derived from
third-party sources under their own terms — see [`DATA_LICENSE.md`](DATA_LICENSE.md).
