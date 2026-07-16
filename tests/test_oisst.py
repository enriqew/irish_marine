"""OISST monthly SST grid reshaping (process_oisst)."""

import json

import process


def _write_raw(tmp_path, records, months):
    raw = {
        "resolution_deg": 0.25,
        "box": {"lat_min": 51, "lat_max": 56, "lng_min": -11, "lng_max": -5},
        "months": months,
        "records": records,
    }
    (tmp_path / "raw_oisst.json").write_text(json.dumps(raw), encoding="utf-8")


def test_oisst_builds_per_cell_time_series(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA_DIR", tmp_path)
    _write_raw(
        tmp_path,
        records=[
            {"month": "2020-01", "lat": 53.125, "lng": -6.125, "sst": 9.0},
            {"month": "2020-07", "lat": 53.125, "lng": -6.125, "sst": 15.0},
        ],
        months=["2020-01", "2020-07"],
    )
    grid = process.process_oisst()

    assert grid["months"] == ["2020-01", "2020-07"]
    assert len(grid["cells"]) == 1
    cell = grid["cells"][0]
    # temps are aligned to the shared months list
    assert cell["temps"] == [9.0, 15.0]
    assert cell["mean"] == 12.0


def test_oisst_aligns_missing_months_as_null(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA_DIR", tmp_path)
    # Cell B is missing the second month -> that slot must be null, not shifted.
    _write_raw(
        tmp_path,
        records=[
            {"month": "2020-01", "lat": 53.125, "lng": -6.125, "sst": 9.0},
            {"month": "2020-02", "lat": 53.125, "lng": -6.125, "sst": 8.0},
            {"month": "2020-01", "lat": 54.125, "lng": -7.125, "sst": 10.0},
        ],
        months=["2020-01", "2020-02"],
    )
    grid = process.process_oisst()
    by_lat = {c["lat"]: c for c in grid["cells"]}
    assert by_lat[53.125]["temps"] == [9.0, 8.0]
    assert by_lat[54.125]["temps"] == [10.0, None]


def test_oisst_temp_range_spans_all_cell_months(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA_DIR", tmp_path)
    _write_raw(
        tmp_path,
        records=[
            {"month": "2020-01", "lat": 53.125, "lng": -6.125, "sst": 8.0},
            {"month": "2020-07", "lat": 53.125, "lng": -6.125, "sst": 16.0},
        ],
        months=["2020-01", "2020-07"],
    )
    grid = process.process_oisst()
    assert grid["temp_min"] == 8.0
    assert grid["temp_max"] == 16.0


def test_oisst_drops_unphysical_values(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA_DIR", tmp_path)
    _write_raw(
        tmp_path,
        records=[
            {"month": "2020-01", "lat": 53.125, "lng": -6.125, "sst": 11.0},
            {"month": "2020-01", "lat": 53.125, "lng": -6.125, "sst": 9999.0},
        ],
        months=["2020-01"],
    )
    grid = process.process_oisst()
    assert grid["cells"][0]["temps"] == [11.0]


def test_oisst_missing_raw_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "DATA_DIR", tmp_path)
    grid = process.process_oisst()
    assert grid["cells"] == []
    assert grid["months"] == []
