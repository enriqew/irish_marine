# Data sources & attribution

The source **code** in this repository is licensed under the MIT License (see
`LICENSE`). The processed **data** in `output/` is derived from the third-party
sources below, each under its own terms. If you redistribute the artefacts,
keep the attributions.

| Source | Used for | Licence | Attribution |
|---|---|---|---|
| **OBIS** — Ocean Biodiversity Information System, aggregation API (`api.obis.org`) | Curated species list + taxonomy + record totals (`species.json`), global density backdrop (`density.json`) | CC BY 4.0 | OBIS (2026) Ocean Biodiversity Information System. Intergovernmental Oceanographic Commission of UNESCO. https://obis.org |
| **OBIS** — raw occurrence dump (AWS Open Data GeoParquet, `s3://obis-open-data/occurrence`) | Per-species 0.1° footprints (`footprints/*.json`), monthly frames (`monthly/*.json`), annual counts, cell index (`cells.json`) | CC BY 4.0 | Same as above; the GeoParquet export on AWS Open Data is OBIS's sanctioned bulk-access path. https://obis.org/data/access/ |
| **NOAA** — Optimum Interpolation SST (OISST) v2.1, monthly means (served by NOAA PSL OPeNDAP) | Per-species SST series + thermal niches (`series_sst` / `niche` in `species.json`), SST raster tiles (`sst/*.png`, `sst/field.json`) | Public domain (U.S. Government work) | NOAA/NCEI OISST v2.1, monthly product via NOAA PSL. Huang et al. (2021), J. Climate. https://psl.noaa.gov/data/gridded/data.noaa.oisst.v2.highres.html |

## Notes

- Individual OBIS occurrence records originate from thousands of contributing
  datasets, each with its own citation. The aggregates here (counts per 0.1°
  cell — no verbatim records, no dataset-level fields) are redistributed under
  CC BY 4.0 with attribution to OBIS; consult the OBIS portal for dataset-level
  citations.
- NOAA OISST is a U.S. Government work in the public domain, with no use
  restrictions. Attribution is a courtesy, and the data carry the standard NOAA
  disclaimer that they are not intended for legal/navigational use. The tiles
  and per-species series are monthly means (2015→present) on the global 0.25°
  grid (strided to ~0.5° for the join).
