"""Taxonomic grouping and date parsing — the logic most prone to silent errors."""

import pandas as pd
import pytest

import process


# --- _classify_group ---------------------------------------------------------
# These cases mirror the exact rank values OBIS/WoRMS emits. They are a
# regression guard for the bug where sharks (class Elasmobranchii) and
# cetaceans (order Cetartiodactyla) were both mis-bucketed as "other".

@pytest.mark.parametrize("row, expected", [
    # Cetaceans — order Cetartiodactyla, NOT an order literally named "Cetacea"
    ({"class": "Mammalia", "order": "Cetartiodactyla", "family": "Phocoenidae"}, "cetacean"),
    ({"class": "Mammalia", "order": "Cetartiodactyla", "family": "Delphinidae"}, "cetacean"),
    ({"class": "Mammalia", "order": "Cetacea", "family": "Balaenopteridae"}, "cetacean"),
    # Sharks / rays / skates / chimaeras — class Elasmobranchii / Holocephali
    ({"class": "Elasmobranchii", "order": "Carcharhiniformes", "family": "Scyliorhinidae"}, "shark"),
    ({"class": "Elasmobranchii", "order": "Rajiformes", "family": "Rajidae"}, "shark"),
    ({"class": "Holocephali", "order": "Chimaeriformes", "family": "Chimaeridae"}, "shark"),
    ({"class": "Chondrichthyes", "order": "", "family": ""}, "shark"),
    # Seals — family-based (Carnivora), must win over the cetacean order test
    ({"class": "Mammalia", "order": "Carnivora", "family": "Phocidae"}, "seal"),
    # Bony fish
    ({"class": "Teleostei", "order": "Gadiformes", "family": "Gadidae"}, "fish"),
    ({"class": "Actinopterygii", "order": "Perciformes", "family": "Labridae"}, "fish"),
    # Seabirds
    ({"class": "Aves", "order": "Charadriiformes", "family": "Laridae"}, "seabird"),
    # Invertebrates / plankton fall through to "other"
    ({"class": "Polychaeta", "order": "", "family": ""}, "other"),
    ({"class": None, "order": None, "family": None}, "other"),
])
def test_classify_group(row, expected):
    assert process._classify_group(row) == expected


def test_classify_is_case_insensitive():
    assert process._classify_group({"class": "ELASMOBRANCHII"}) == "shark"
    assert process._classify_group({"order": "cetartiodactyla"}) == "cetacean"


def test_every_group_is_valid():
    """No classification may produce a group outside the agreed vocabulary."""
    sample = [
        {"class": "Mammalia", "order": "Cetartiodactyla"},
        {"class": "Elasmobranchii"},
        {"family": "Phocidae"},
        {"class": "Teleostei"},
        {"class": "Aves"},
        {"class": "Anthozoa"},
    ]
    for row in sample:
        assert process._classify_group(row) in process.VALID_GROUPS


# --- _parse_obis_date --------------------------------------------------------

def test_parse_date_unix_millis():
    # 2020-01-01T00:00:00Z in milliseconds
    assert process._parse_obis_date(1_577_836_800_000) == "2020-01-01"


def test_parse_date_iso_string():
    assert process._parse_obis_date("2015-06-30T12:00:00") == "2015-06-30"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf")])
def test_parse_date_missing_returns_none(bad):
    assert process._parse_obis_date(bad) is None


def test_parse_date_pre_1970_negative_millis():
    # Historical OBIS specimens carry negative ms timestamps. These must parse
    # to a real date, not fall through to the string fallback (which used to
    # leak the raw number, e.g. "-139449600"). Regression for the Windows
    # OSError-on-negative-fromtimestamp bug.
    assert process._parse_obis_date(-139_449_600_000) == "1965-08-01"
    assert process._parse_obis_date(0) == "1970-01-01"


def test_parse_date_never_leaks_raw_timestamp():
    out = process._parse_obis_date(-139_449_600_000)
    assert not out.startswith("-")


# --- process_seasonal --------------------------------------------------------

def test_seasonal_summary_counts_by_month_and_group():
    sightings = [
        {"date": "2021-03-04", "group": "cetacean"},
        {"date": "2021-03-20", "group": "cetacean"},
        {"date": "2021-07-01", "group": "shark"},
    ]
    summary = process.process_seasonal(sightings)
    by_key = {(r["month"], r["group"]): r["sighting_count"] for r in summary}
    assert by_key[(3, "cetacean")] == 2
    assert by_key[(7, "shark")] == 1


def test_seasonal_summary_empty_input():
    assert process.process_seasonal([]) == []
