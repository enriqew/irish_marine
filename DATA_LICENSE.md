# Data sources & attribution

The source **code** in this repository is licensed under the MIT License (see
`LICENSE`). The processed **data** in `output/` is derived from the third-party
sources below, each under its own terms. If you redistribute the artefacts,
keep the attributions.

| Source | Used for | Licence | Attribution |
|---|---|---|---|
| **OBIS** — Ocean Biodiversity Information System | `sightings.json`, `species.json`, `seasonal-summary.json` | CC BY 4.0 | OBIS (2026) Ocean Biodiversity Information System. Intergovernmental Oceanographic Commission of UNESCO. https://obis.org |
| **Marine Institute Ireland** — Irish Weather Buoy Network (ERDDAP `IWBNetwork`) | `sea-temperature.json` | CC BY 4.0 | Contains Irish public sector data from the Marine Institute, licensed under CC BY 4.0. https://erddap.marine.ie |
| **NOAA** — Optimum Interpolation SST (OISST) v2.1, monthly means (served by NOAA PSL) | `sst-grid.json` | Public domain (U.S. Government work) | NOAA/NCEI OISST v2.1, monthly product via NOAA PSL. Huang et al. (2021), J. Climate. https://psl.noaa.gov/data/gridded/data.noaa.oisst.v2.highres.html |
| **EEA** — Natura 2000 (end 2022) | `protected-areas.json` | EEA standard reuse policy (CC BY 4.0) | © European Environment Agency (EEA). Natura 2000 data. https://www.eea.europa.eu |
| **Natural Earth** — 1:10m land polygons | Coastline reference for the marine/coastal filter (not published) | Public domain | Made with Natural Earth. https://www.naturalearthdata.com |

## Notes

- Individual OBIS occurrence records originate from many contributing datasets,
  each with its own citation. The aggregate is redistributed here under CC BY 4.0
  with attribution to OBIS; consult the OBIS portal for dataset-level citations.
- The Natural Earth land polygon is only used at build time to filter protected
  areas to coastal/marine sites. It is not included in the published artefacts.
- NOAA OISST is a U.S. Government work in the public domain, with no use
  restrictions. Attribution is a courtesy, and the data carry the standard NOAA
  disclaimer that they are not intended for legal/navigational use. `sst-grid.json`
  is a monthly-mean time series (2015→present) subset to the Ireland box at 0.25°.
