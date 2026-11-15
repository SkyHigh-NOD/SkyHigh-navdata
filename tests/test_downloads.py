"""Base downloads: retries, truncated responses, and a source that gives nothing."""
import http.client
import json
import urllib.error

import pytest

from skyhigh_navdata import base, regions

CSV = (
    "id,ident,type,name,latitude_deg,longitude_deg,iso_country,icao_code,gps_code\n"
    "1,EGLL,large_airport,Heathrow,51.47,-0.45,GB,EGLL,EGLL\n"
)


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def failing_then(monkeypatch, errors, body=b"ok"):
    """urlopen that raises each of ``errors`` in turn, then answers."""
    calls = []
    queue = list(errors)

    def urlopen(request, timeout=None):
        calls.append(request.full_url)
        if queue:
            raise queue.pop(0)
        return Response(body)

    monkeypatch.setattr(base.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(base, "RETRY_PAUSE", 0)
    return calls


def test_a_download_that_fails_once_succeeds_on_the_second_attempt(monkeypatch):
    calls = failing_then(monkeypatch, [urllib.error.URLError("reset")])
    assert base._fetch("https://example.invalid/x") == b"ok"
    assert len(calls) == 2


def test_a_truncated_response_is_asked_for_again(monkeypatch):
    calls = failing_then(monkeypatch, [http.client.IncompleteRead(b"half")])
    assert base._fetch("https://example.invalid/x") == b"ok"
    assert len(calls) == 2


def test_a_download_gives_up_after_three_attempts(monkeypatch):
    calls = failing_then(monkeypatch, [urllib.error.URLError("down")] * 5)
    with pytest.raises(urllib.error.URLError):
        base._fetch("https://example.invalid/x")
    assert len(calls) == 3


def test_a_missing_file_is_not_asked_for_three_times(monkeypatch):
    missing = urllib.error.HTTPError("https://example.invalid/x", 404, "Not Found", None, None)
    calls = failing_then(monkeypatch, [missing])
    with pytest.raises(urllib.error.HTTPError):
        base._fetch("https://example.invalid/x")
    assert len(calls) == 1


def feature(properties, lon, lat):
    return {"properties": properties, "geometry": {"coordinates": [lon, lat]}}


def cached_export(tmp_path, key="gb_apt.ndgeojson"):
    path = base._export_path(tmp_path, key)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(feature({"icaoCode": "EGLL"}, -0.45, 51.47)) + "\n", encoding="utf-8")
    (tmp_path / "openaip.json").write_text(json.dumps({key: "old"}), encoding="utf-8")
    return key


def truncated(*args, **kwargs):
    raise http.client.IncompleteRead(b"half")


def test_a_truncated_export_falls_back_to_the_cached_copy(tmp_path, monkeypatch):
    key = cached_export(tmp_path)
    monkeypatch.setattr(base, "_openaip_listing", lambda cache_dir, log=None: {
        key: {"url": "https://example.invalid/" + key, "last_modified": "new"}})
    monkeypatch.setattr(base, "_download_to", truncated)
    assert "EGLL" in base.load_openaip(tmp_path)["airports"]
    assert json.loads((tmp_path / "openaip.json").read_text(encoding="utf-8")) == {key: "old"}


def test_a_truncated_listing_falls_back_to_the_cached_exports(tmp_path, monkeypatch):
    cached_export(tmp_path)
    monkeypatch.setattr(base, "_openaip_listing", truncated)
    monkeypatch.setattr(base, "_download_to", lambda *a, **k: pytest.fail("no download expected"))
    assert "EGLL" in base.load_openaip(tmp_path)["airports"]


def test_openaip_exports_with_nothing_in_them_are_an_error(tmp_path, monkeypatch):
    key = "gb_apt.ndgeojson"
    monkeypatch.setattr(base, "_openaip_listing", lambda cache_dir, log=None: {
        key: {"url": "https://example.invalid/" + key, "last_modified": "new"}})
    monkeypatch.setattr(base, "_download_to", truncated)
    with pytest.raises(base.BaseDataError, match="openAIP"):
        base.load_openaip(tmp_path)


def test_a_truncated_ourairports_download_uses_the_cached_csv(tmp_path, monkeypatch):
    (tmp_path / "ourairports.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "ourairports.json").write_text(json.dumps({"sha": "old"}), encoding="utf-8")
    monkeypatch.setattr(base, "_fetch", lambda url, timeout=120: json.dumps([{"sha": "new"}]).encode())
    monkeypatch.setattr(base, "_download_to", truncated)
    assert "EGLL" in base.load_ourairports(tmp_path)["airports"]
    assert json.loads((tmp_path / "ourairports.json").read_text(encoding="utf-8")) == {"sha": "old"}


def test_ourairports_with_no_download_and_no_cache_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_fetch", truncated)
    with pytest.raises(base.BaseDataError, match="OurAirports"):
        base.load_ourairports(tmp_path)


def test_ourairports_is_fetched_from_the_branch_when_the_revision_lookup_fails(tmp_path, monkeypatch):
    def fetch(url, timeout=120):
        if "api.github.com" in url:
            raise urllib.error.URLError("rate limited")
        assert "/%s/" % base.OURAIRPORTS_BRANCH in url
        return CSV.encode("utf-8")

    monkeypatch.setattr(base, "_fetch", fetch)
    assert "EGLL" in base.load_ourairports(tmp_path)["airports"]
    assert not (tmp_path / "ourairports.json").exists()


def test_an_ourairports_csv_without_aerodromes_is_an_error(tmp_path, monkeypatch):
    (tmp_path / "ourairports.csv").write_text(CSV.splitlines()[0] + "\n", encoding="utf-8")
    monkeypatch.setattr(base, "_fetch", truncated)
    with pytest.raises(base.BaseDataError):
        base.load_ourairports(tmp_path)


def test_require_base_refuses_an_empty_source():
    openaip = {"airports": {"LFPG": {}}, "navaids": {}, "airspaces": []}
    ourairports = {"airports": {"EGLL": {}}}
    assert base.require_base(openaip, ourairports)["ourairports"] is ourairports
    with pytest.raises(base.BaseDataError, match="OurAirports"):
        base.require_base(openaip, {"airports": {}, "counts": {}})
    with pytest.raises(base.BaseDataError, match="openAIP"):
        base.require_base({"airports": {}, "navaids": {}, "airspaces": []}, ourairports)


def test_the_country_map_is_written_from_openaip_countries_alone(tmp_path):
    openaip_airports = {"LFPG": {"country": "FR"}, "LFPO": {"country": "FR"}, "EGLL": {"country": "GB"}}
    assert regions.write_icao_country_map(tmp_path, None, openaip_airports) == 2
    lines = [line for line in (tmp_path / "OAIP-countries").read_text(encoding="utf-8").splitlines()
             if not line.startswith("#")]
    assert lines == ["EG GB", "LF FR"]


def test_an_aerodrome_in_both_sources_votes_once(tmp_path):
    ourairports = {"airports": {"EGAA": {"country": "IE"}, "EGLL": {"country": "IE"}}, "counts": {}}
    openaip_airports = {"EGLL": {"country": "GB"}, "EGKK": {"country": "GB"}}
    regions.write_icao_country_map(tmp_path, ourairports, openaip_airports)
    assert "EG GB" in (tmp_path / "OAIP-countries").read_text(encoding="utf-8")


def test_openaip_airspace_gets_its_country_map_without_ourairports(tmp_path):
    square = [(50.0, -1.0), (50.0, 0.0), (51.0, 0.0), (50.0, -1.0)]
    data = {
        "airspaces": [{"name": "LONDON CTR", "upper": "2500 ft", "lower": "GND",
                       "rings": [square], "country": "GB"}],
        "airports": {"EGLL": {"country": "GB"}},
    }
    assert regions.write_openaip_airspace(tmp_path, data, "world", "11/26") == 1
    assert "EG GB" in (tmp_path / "OAIP-countries").read_text(encoding="utf-8")
