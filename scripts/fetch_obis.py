#!/usr/bin/env python3
"""
fetch_obis.py — Build the curated global species LIST + a global density backdrop
from OBIS aggregation endpoints (no bulk occurrence download).

OBIS holds ~201 M occurrence records / ~168 k species. Downloading raw records
globally is ~400 GB, so we use OBIS's server-side aggregation:

  * /checklist?scientificname=<taxon>   → species under a taxon, each with a
        total ``records`` count and full WoRMS taxonomy. Used to assemble a
        curated, data-driven species list (top-N by records per group).
  * /occurrence/grid/2 (all species)    → one coarse global density backdrop.

Output: data/obis_species.json — the curated list with taxonomy + record totals,
plus the density backdrop. The per-species footprint (``cells``) and annual counts
(``year_counts``) are added afterwards by ``build_footprints_raw.py`` from the raw
OBIS dump (a finer 0.1° grid than the old grid API gave), so they are NOT fetched
here. ``process.py`` then joins SST and emits the frontend artifacts.

API docs: https://api.obis.org
"""

import json
import time
from pathlib import Path

import httpx

BASE = "https://api.obis.org/v3"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- Snapshot scope -------------------------------------------------------
# Curated higher taxa → display group, with how many top species (by all-time
# record count) to keep from each. Tuned to land ~300-800 species total across a
# balanced, charismatic-plus-notable global set. Edit freely; the pipeline is
# fully driven by this table.
CURATED_TAXA: list[tuple[str, str, int]] = [
    # (taxon queried on /checklist, display group, top-N species by records)
    ("Cetacea", "cetacean", 90),          # whales, dolphins, porpoises
    ("Elasmobranchii", "shark", 160),     # sharks, rays, skates
    ("Holocephali", "shark", 20),         # chimaeras
    ("Phocidae", "seal", 20),             # true seals
    ("Otariidae", "seal", 16),            # fur seals, sea lions
    ("Odobenidae", "seal", 2),            # walrus
    ("Sphenisciformes", "seabird", 20),   # penguins
    ("Procellariiformes", "seabird", 45), # albatrosses, petrels, shearwaters
    ("Suliformes", "seabird", 20),        # gannets, cormorants, frigatebirds
    ("Pelecaniformes", "seabird", 15),    # pelicans, herons (coastal)
    ("Alcidae", "seabird", 15),           # auks, puffins, guillemots
    ("Laridae", "seabird", 20),           # gulls, terns
    ("Scombridae", "fish", 30),           # tuna, mackerel
    ("Clupeidae", "fish", 25),            # herring, sardines
    ("Gadidae", "fish", 20),              # cod, haddock, whiting
    ("Salmonidae", "fish", 15),           # salmon, trout (anadromous)
    ("Pleuronectiformes", "fish", 30),    # flatfish
    ("Istiophoridae", "fish", 10),        # marlins, sailfish
    ("Xiphiidae", "fish", 2),             # swordfish
    ("Molidae", "fish", 4),               # ocean sunfish
    ("Syngnathidae", "fish", 25),         # seahorses, pipefish
    ("Carangidae", "fish", 25),           # jacks, trevallies
    ("Epinephelidae", "fish", 20),        # groupers
    ("Lophiidae", "fish", 6),             # anglerfish / monkfish
    ("Testudines", "other", 8),           # sea turtles
    ("Octopoda", "other", 15),            # octopuses
]

START_YEAR = 2015          # annual count axis start (aligns with the OISST era)
PAGE_SIZE = 500            # /checklist pagination
POLITE_SLEEP = 0.25        # seconds between requests
RETRY_WAIT = 3.0


_RETRIES = 6


def _get(client: httpx.Client, path: str, params: dict | None = None) -> dict | list:
    url = f"{BASE}/{path}"
    for attempt in range(_RETRIES):
        try:
            resp = client.get(url, params=params or {}, timeout=120)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_WAIT * (attempt + 1)
                print(f"    HTTP {resp.status_code}; waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        # TransportError covers timeouts AND connection drops (RemoteProtocolError,
        # ConnectError, ReadError…) — OBIS occasionally disconnects mid-response, and
        # a single drop must not kill a multi-hour run. Back off and retry.
        except httpx.TransportError as exc:
            if attempt == _RETRIES - 1:
                raise
            wait = 2 * (attempt + 1)
            print(f"    {type(exc).__name__} (attempt {attempt + 1}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"failed after retries: {url}")


# ---------------------------------------------------------------------------
# 1. Build the curated species list from /checklist
# ---------------------------------------------------------------------------

def _checklist_species(client: httpx.Client, taxon: str) -> list[dict]:
    """All accepted marine *species* under a taxon, each with a record count."""
    out: list[dict] = []
    skip = 0
    while True:
        payload = _get(client, "checklist", {
            "scientificname": taxon, "size": PAGE_SIZE, "skip": skip,
        })
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not results:
            break
        for r in results:
            if r.get("taxonRank") != "Species":
                continue
            if r.get("taxonomicStatus") != "accepted":
                continue
            if not r.get("is_marine") and not r.get("is_brackish"):
                continue
            out.append(r)
        skip += len(results)
        total = payload.get("total", 0) if isinstance(payload, dict) else 0
        if skip >= total:
            break
        time.sleep(POLITE_SLEEP)
    return out


def build_species_list(client: httpx.Client) -> list[dict]:
    seen: dict[int, dict] = {}   # taxonID -> record, dedup across taxa
    for taxon, group, top_n in CURATED_TAXA:
        print(f"  checklist: {taxon} (group={group}, top {top_n})")
        species = _checklist_species(client, taxon)
        species.sort(key=lambda r: r.get("records", 0), reverse=True)
        kept = 0
        for r in species[:top_n]:
            tid = r.get("taxonID")
            if tid is None or tid in seen:
                continue
            seen[tid] = {
                "taxon_id": tid,
                "aphia_id": tid,   # OBIS taxonID == WoRMS AphiaID
                "scientific_name": r.get("scientificName"),
                "group": group,
                "records_total": r.get("records", 0),
                "taxonomy": {
                    "phylum": r.get("phylum"),
                    "class": r.get("class"),
                    "order": r.get("order"),
                    "family": r.get("family"),
                },
            }
            kept += 1
        print(f"    kept {kept} species (of {len(species)} marine)")
        time.sleep(POLITE_SLEEP)
    return sorted(seen.values(), key=lambda x: x["records_total"], reverse=True)


# ---------------------------------------------------------------------------
# 2. Global all-species density backdrop (one request)
# ---------------------------------------------------------------------------

def _cell_centroid(feature: dict) -> tuple[float, float]:
    """[lng, lat] centre of a grid-cell polygon's bounding box."""
    ring = feature["geometry"]["coordinates"][0]
    lngs = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return (
        round((min(lngs) + max(lngs)) / 2, 4),
        round((min(lats) + max(lats)) / 2, 4),
    )

def fetch_density(client: httpx.Client) -> list[list]:
    """Coarse global grid of ALL-species record counts, for the idle backdrop."""
    print("  density: global all-species grid (precision 2)")
    payload = _get(client, "occurrence/grid/2")
    cells = []
    for f in payload.get("features", []):
        n = f.get("properties", {}).get("n", 0)
        if not n:
            continue
        lng, lat = _cell_centroid(f)
        cells.append([lng, lat, int(n)])
    return cells


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True, headers={"User-Agent": "marine-atlas-pipeline"}) as client:
        print("Building curated species list from OBIS /checklist...")
        species = build_species_list(client)
        print(f"  -> {len(species)} curated species\n")

        density = fetch_density(client)

    out = {
        "generated": time.strftime("%Y-%m-%d"),
        "start_year": START_YEAR,
        # Per-species `cells` + `year_counts` are added by build_footprints_raw.py.
        "species": species,
        "density": density,
    }
    out_path = DATA_DIR / "obis_species.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\n  Saved -> {out_path}  ({size_mb:.1f} MB, {len(species)} species, "
          f"{len(density)} density cells)")
    print("  Next: build_footprints_raw.py (footprints + annual + monthly from raw dump)")


if __name__ == "__main__":
    main()
