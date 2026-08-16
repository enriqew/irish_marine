"""Unit tests for the aggregation pipeline (process.py + build_footprints_raw.py).

These replace the Ireland-era suite (test_classification / test_geo_and_dedup /
test_oisst), which tested functions deleted in the global re-architecture. Here we
test what the pipeline actually does now: the SST sampler/join, the cell dedup index,
NaN sanitisation, and the raw-footprint rollup. Nothing here touches the 91 GB dump —
the DuckDB results are simulated with small in-memory DataFrames.
"""

import numpy as np
import pandas as pd
import pytest

import process
import build_footprints_raw as bfr


# ---------------------------------------------------------------------------
# process._round — NaN/Inf sanitisation
# ---------------------------------------------------------------------------

def test_round_normal_value():
    assert process._round(12.345) == 12.3
    assert process._round(12.345, 2) == 12.34 or process._round(12.345, 2) == 12.35


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_round_bad_returns_none(bad):
    assert process._round(bad) is None


# ---------------------------------------------------------------------------
# process.CellIndex — dedup centroids into a dense id space
# ---------------------------------------------------------------------------

def test_cell_index_dedups_same_rounded_coord():
    idx = process.CellIndex()
    a = idx.get(-6.0, 53.5)
    b = idx.get(-6.00001, 53.50001)   # same to 4 dp → same cell
    assert a == b
    assert len(idx.centroids) == 1


def test_cell_index_distinct_coords_get_new_ids():
    idx = process.CellIndex()
    a = idx.get(-6.0, 53.5)
    b = idx.get(-8.0, 54.5)
    assert a == 0 and b == 1
    assert idx.centroids == [[-6.0, 53.5], [-8.0, 54.5]]


# ---------------------------------------------------------------------------
# process._weighted_quantile
# ---------------------------------------------------------------------------

def test_weighted_quantile_equal_weights_matches_median():
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    w = np.ones_like(vals)
    assert process._weighted_quantile(vals, w, 0.5) == pytest.approx(3.0)


def test_weighted_quantile_skews_toward_heavy_weight():
    vals = np.array([0.0, 10.0])
    # Almost all weight on the low value → the 0.5 quantile sits near 0, not 5.
    w = np.array([9.0, 1.0])
    assert process._weighted_quantile(vals, w, 0.5) < 2.0


# ---------------------------------------------------------------------------
# process.SstGrid — nearest-cell sampling + count-weighted series/niche
# ---------------------------------------------------------------------------

def _make_grid(tmp_path):
    """A tiny 2-month, 3×2 synthetic OISST grid saved as an .npz."""
    months = np.array(["2015-01", "2015-07"])
    lats = np.array([0.0, 1.0, 2.0], dtype="float32")
    lons = np.array([10.0, 11.0], dtype="float32")
    # sst[t, y, x]: winter cold, summer warm; y increases north.
    sst = np.array([
        [[5.0, 6.0], [7.0, 8.0], [9.0, 10.0]],       # 2015-01
        [[15.0, 16.0], [17.0, 18.0], [19.0, 20.0]],  # 2015-07
    ], dtype="float32")
    p = tmp_path / "oisst_grid.npz"
    np.savez(p, months=months, lats=lats, lons=lons, sst=sst)
    return p


def test_sstgrid_nearest_cell_indices(tmp_path):
    grid = process.SstGrid(_make_grid(tmp_path))
    yi, xi = grid.cell_indices(np.array([10.2, 10.9]), np.array([0.4, 1.6]))
    # lat 0.4 → row 0 (0.0); lat 1.6 → row 2 (2.0)
    assert list(yi) == [0, 2]
    # lng 10.2 → col 0 (10.0); lng 10.9 → col 1 (11.0)
    assert list(xi) == [0, 1]


def test_sstgrid_series_over_single_cell(tmp_path):
    grid = process.SstGrid(_make_grid(tmp_path))
    yi, xi = grid.cell_indices(np.array([10.0]), np.array([0.0]))  # bottom-left cell
    series, mean, p10, p90, amp = grid.series_over_cells(yi, xi, np.array([1.0]))
    # That cell reads 5 °C in Jan, 15 °C in Jul.
    assert series[0] == pytest.approx(5.0)
    assert series[1] == pytest.approx(15.0)
    assert mean == pytest.approx(10.0)          # all-time cell mean (5,15)
    assert amp == pytest.approx(10.0)           # 15 − 5


def test_sstgrid_series_count_weighted_across_cells(tmp_path):
    grid = process.SstGrid(_make_grid(tmp_path))
    # Two cells, one weighted 3× the other → weighted Jan mean pulled toward it.
    yi = np.array([0, 2]); xi = np.array([0, 0])   # 5 °C cell and 9 °C cell (Jan)
    series, mean, *_ = grid.series_over_cells(yi, xi, np.array([3.0, 1.0]))
    assert series[0] == pytest.approx((5 * 3 + 9 * 1) / 4)   # 6.0


# ---------------------------------------------------------------------------
# build_footprints_raw.rollup — DuckDB results → per-species shapes
# ---------------------------------------------------------------------------

def _agg_frames():
    """Small stand-ins for the three DuckDB aggregations, two species."""
    foot = pd.DataFrame({
        "aphiaid": [100, 100, 200],
        "glng":    [-6.0, -6.1, 12.3],
        "glat":    [53.5, 53.5, -40.0],
        "n":       [40, 10, 7],
    })
    yr = pd.DataFrame({
        "aphiaid": [100, 100, 200],
        "year":    [2015, 2016, 2015],
        "n":       [30, 20, 7],
    })
    mon = pd.DataFrame({
        "aphiaid": [100, 100, 200],
        "glng":    [-6.0, -6.0, 12.3],
        "glat":    [53.5, 53.5, -40.0],
        "year":    [2015, 2016, 2015],
        "m":       [1, 7, 3],
        "n":       [12, 8, 7],
    })
    return foot, yr, mon


def test_rollup_all_time_footprint():
    footprints, _, _ = bfr.rollup(*_agg_frames())
    assert footprints[100] == [[-6.0, 53.5, 40], [-6.1, 53.5, 10]]
    assert footprints[200] == [[12.3, -40.0, 7]]


def test_rollup_year_counts_are_string_keyed():
    _, year_counts, _ = bfr.rollup(*_agg_frames())
    assert year_counts[100] == {"2015": 30, "2016": 20}
    assert year_counts[200] == {"2015": 7}


def test_rollup_monthly_frames_keyed_by_year_month():
    _, _, monthly = bfr.rollup(*_agg_frames())
    assert monthly[100] == {"2015-01": [[-6.0, 53.5, 12]],
                            "2016-07": [[-6.0, 53.5, 8]]}
    assert monthly[200] == {"2015-03": [[12.3, -40.0, 7]]}


def test_rollup_counts_are_native_ints():
    """JSON must not choke on numpy ints from the DataFrame."""
    footprints, year_counts, monthly = bfr.rollup(*_agg_frames())
    assert all(isinstance(c[2], int) for c in footprints[100])
    assert all(isinstance(v, int) for v in year_counts[100].values())
    assert all(isinstance(cell[2], int)
               for frames in monthly.values()
               for cells in frames.values() for cell in cells)
