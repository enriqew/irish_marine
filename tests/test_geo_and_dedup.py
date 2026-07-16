"""Geometry filtering and sighting de-duplication."""

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

import process


# --- _dedup_sightings --------------------------------------------------------

def test_dedup_collapses_same_species_date_and_location():
    df = pd.DataFrame([
        {"lat": 53.5000, "lng": -6.0000, "species": "Phocoena phocoena", "date": "2021-01-01"},
        # Same species/date, 0.002 deg away -> same 0.01 deg bucket -> duplicate
        {"lat": 53.5020, "lng": -6.0010, "species": "Phocoena phocoena", "date": "2021-01-01"},
    ])
    out = process._dedup_sightings(df)
    assert len(out) == 1
    # The collapsed count is preserved so the map can weight the marker
    assert out.iloc[0]["observations"] == 2


def test_dedup_observations_count_is_per_group():
    df = pd.DataFrame([
        {"lat": 53.5, "lng": -6.0, "species": "A", "date": "2021-01-01"},
        {"lat": 53.5, "lng": -6.0, "species": "A", "date": "2021-01-01"},
        {"lat": 53.5, "lng": -6.0, "species": "A", "date": "2021-01-01"},
        {"lat": 54.5, "lng": -8.0, "species": "B", "date": "2021-01-01"},
    ])
    out = process._dedup_sightings(df).set_index("species")
    assert out.loc["A", "observations"] == 3
    assert out.loc["B", "observations"] == 1


def test_dedup_keeps_distinct_locations_and_dates():
    df = pd.DataFrame([
        {"lat": 53.5, "lng": -6.0, "species": "Phocoena phocoena", "date": "2021-01-01"},
        {"lat": 54.5, "lng": -8.0, "species": "Phocoena phocoena", "date": "2021-01-01"},
        {"lat": 53.5, "lng": -6.0, "species": "Phocoena phocoena", "date": "2021-06-01"},
    ])
    out = process._dedup_sightings(df)
    assert len(out) == 3


# --- _filter_marine ----------------------------------------------------------

def _gdf(*geoms):
    return gpd.GeoDataFrame({"geometry": list(geoms)}, crs="EPSG:4326")


def test_filter_marine_drops_inland_and_keeps_coastal():
    # Land occupies lon 0..1; sea is everything to the east.
    land = box(0, 0, 1, 1)

    inland = box(0.4, 0.4, 0.5, 0.5)      # deep inside land, far from coast
    offshore = box(1.4, 0.4, 1.5, 0.5)    # fully at sea
    coastal = box(0.9, 0.4, 1.1, 0.5)     # straddles the coastline

    gdf = _gdf(inland, offshore, coastal)
    kept = process._filter_marine(gdf, land)

    kept_geoms = list(kept.geometry)
    assert offshore in kept_geoms
    assert coastal in kept_geoms
    assert inland not in kept_geoms


def test_filter_marine_returns_geodataframe():
    land = box(0, 0, 1, 1)
    gdf = _gdf(box(1.4, 0.4, 1.5, 0.5))
    kept = process._filter_marine(gdf, land)
    assert isinstance(kept, gpd.GeoDataFrame)
