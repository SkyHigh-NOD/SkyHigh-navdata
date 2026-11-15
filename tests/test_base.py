import json
import urllib.error

import pytest

from skyhigh_navdata import base


def feature(kind_props, lon, lat):
    return {"properties": kind_props, "geometry": {"coordinates": [lon, lat]}}


def absorb(key, record):
    airports, navaids, points, airspaces = {}, {}, {}, []
    base._absorb_openaip_record(key, record, airports, navaids, points, airspaces)
    return airports, navaids, points, airspaces


def absorb_all(pairs):
    airports, navaids, points, airspaces = {}, {}, {}, []
    for key, record in pairs:
        base._absorb_openaip_record(key, record, airports, navaids, points, airspaces)
    return airports, navaids, points, airspaces


def test_navaids_with_one_ident_in_two_countries_are_both_kept():
    _, navaids, _, _ = absorb_all([
        ("au_nav.ndgeojson", feature({"identifier": "LHR", "country": "AU"}, 150.0, -30.0)),
        ("de_nav.ndgeojson", feature({"identifier": "LHR", "country": "DE"}, 10.0, 50.0)),
    ])
    assert sorted(nav["region"] for nav in navaids.values()) == ["AU", "DE"]
    assert {nav["ident"] for nav in navaids.values()} == {"LHR"}


def test_two_navaids_with_one_ident_inside_a_country_are_both_kept():
    _, navaids, _, _ = absorb_all([
        ("gb_nav.ndgeojson", feature({"identifier": "ABC", "country": "GB"}, -1.0, 51.0)),
        ("gb_nav.ndgeojson", feature({"identifier": "ABC", "country": "GB"}, -4.0, 57.0)),
    ])
    assert len(navaids) == 2


def test_a_navaid_without_an_elevation_gets_a_number():
    _, navaids, _, _ = absorb("gb_nav.ndgeojson", feature({"identifier": "ABC"}, -1.0, 51.0))
    assert [nav["elev_ft"] for nav in navaids.values()] == [0]


def test_the_country_falls_back_to_the_export_file():
    _, navaids, _, _ = absorb("fr_nav.ndgeojson", feature({"identifier": "ABC"}, 2.0, 48.0))
    assert [nav["region"] for nav in navaids.values()] == ["FR"]


def test_reporting_points_sharing_a_name_are_all_kept_and_stay_out_of_fixes():
    airports, navaids, points, _ = absorb_all([
        ("fr_rpp.ndgeojson", feature({"name": "N", "country": "FR"}, 2.0, 48.0)),
        ("fr_rpp.ndgeojson", feature({"name": "N", "country": "FR"}, 3.0, 45.0)),
        ("gb_rpp.ndgeojson", feature({"name": "M25 Junction 10", "country": "GB"}, -0.4, 51.3)),
    ])
    assert len(points) == 3
    assert {key[:2] for key in points} == {("FR", "N"), ("GB", "M25 JUNCTION 10")}
    assert not airports and not navaids


@pytest.mark.parametrize("code, kept", [
    ("EGLL", True), ("KJFK", True), ("00AK", False), ("0AK2", False),
    ("EGL", False), ("EGLLX", False), ("", False),
])
def test_only_icao_codes_count_as_openaip_airports(code, kept):
    airports, _, _, _ = absorb("us_apt.ndgeojson", feature({"icaoCode": code, "name": "X"}, -80.0, 30.0))
    assert (code in airports) is kept


def test_a_position_off_the_globe_is_refused():
    airports, _, _, _ = absorb("gb_apt.ndgeojson", feature({"icaoCode": "EGLL"}, -0.4, 151.0))
    assert airports == {}


@pytest.mark.parametrize("coordinates", [[], None, "x", [[]], [[[]]]])
def test_an_airspace_without_a_ring_gives_no_rings(coordinates):
    assert base._polygon_rings(coordinates) == []


def test_an_empty_airspace_does_not_stop_the_load():
    _, _, _, airspaces = absorb("gb_asp.ndgeojson", {"properties": {"name": "X"},
                                                     "geometry": {"coordinates": []}})
    assert airspaces == []


def test_polygon_rings_reads_a_polygon_and_a_multipolygon():
    square = [[0, 0], [1, 0], [1, 1], [0, 0]]
    assert base._polygon_rings([square]) == [[(0, 0), (0, 1), (1, 1), (0, 0)]]
    assert len(base._polygon_rings([[square], [square]])) == 2


CSV = (
    "id,ident,type,name,latitude_deg,longitude_deg,iso_country,icao_code,gps_code\n"
    "1,EGLL,large_airport,Heathrow,51.47,-0.45,GB,EGLL,EGLL\n"
    "2,00AK,small_airport,Lowell Field,59.94,-151.69,US,,00AK\n"
    "3,01CA,heliport,Somewhere,34.0,-118.0,US,,\n"
    "4,FAHS,closed,Hoedspruit,-24.36,31.04,ZA,FAHS,FAHS\n"
    "5,US-0001,small_airport,Gps Only,40.0,-100.0,US,,KXYZ\n"
    "6,XXXX,small_airport,Bad Position,abc,1.0,GB,EGZZ,EGZZ\n"
)


def test_ourairports_uses_the_icao_column_not_the_ident():
    airports = base.parse_ourairports_csv(CSV)
    assert sorted(airports) == ["EGLL", "KXYZ"]
    assert airports["EGLL"]["country"] == "GB"
    assert airports["KXYZ"]["name"] == "Gps Only"


def test_a_failed_download_does_not_record_the_new_stamp(tmp_path, monkeypatch):
    key = "gb_apt.ndgeojson"
    path = base._export_path(tmp_path, key)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(feature({"icaoCode": "EGLL"}, -0.45, 51.47)) + "\n", encoding="utf-8")
    (tmp_path / "openaip.json").write_text(json.dumps({key: "old"}), encoding="utf-8")
    monkeypatch.setattr(base, "_openaip_listing", lambda cache_dir, log=None: {
        key: {"url": "https://example.invalid/" + key, "last_modified": "new"}})

    def refuse(url, target, timeout=300):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(base, "_download_to", refuse)
    data = base.load_openaip(tmp_path)
    assert "EGLL" in data["airports"]
    assert json.loads((tmp_path / "openaip.json").read_text(encoding="utf-8")) == {key: "old"}


def test_the_cache_is_used_when_the_listing_cannot_be_fetched(tmp_path, monkeypatch):
    key = "gb_apt.ndgeojson"
    path = base._export_path(tmp_path, key)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(feature({"icaoCode": "EGLL"}, -0.45, 51.47)) + "\n", encoding="utf-8")
    (tmp_path / "openaip.json").write_text(json.dumps({key: "old"}), encoding="utf-8")

    def refuse(cache_dir, log=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(base, "_openaip_listing", refuse)
    monkeypatch.setattr(base, "_download_to", lambda *a, **k: pytest.fail("no download expected"))
    assert "EGLL" in base.load_openaip(tmp_path)["airports"]


def test_no_listing_and_no_cache_is_an_error(tmp_path, monkeypatch):
    def refuse(cache_dir, log=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(base, "_openaip_listing", refuse)
    with pytest.raises(urllib.error.URLError):
        base.load_openaip(tmp_path)
