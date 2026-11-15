"""The whole make_airac.py run with every download replaced by a stand-in."""
import json
import zipfile

import pytest

pytest.importorskip("bs4")

import make_airac  # noqa: E402

SQUARE = [(50.0, -1.0), (50.0, 0.0), (51.0, 0.0), (50.0, -1.0)]
PARIS = [(48.5, 2.0), (48.5, 3.0), (49.5, 3.0), (48.5, 2.0)]


def fake_nav_build(html_dir, nav_dir, region, cycle, log=None):
    nav_dir.mkdir(parents=True, exist_ok=True)
    (nav_dir / "fix.dat").write_text("51.000000 -1.000000 ABBEW\n", encoding="utf-8")
    (nav_dir / "navaid.dat").write_text(
        "2 51.487000 -0.467000 80 113.60 0 0 LON 0 EG London\n99\n", encoding="utf-8")
    (nav_dir / "AD-positions").write_text(
        "EGLL 51.477500,-0.461389 London Heathrow\n1\n", encoding="utf-8")
    (nav_dir / "awy.dat").write_text("ABBEW EG 11 LON EG 3 N 1 85 244 L6\n", encoding="utf-8")
    return {"counts": {"fixes": 1, "navaids": 1, "aerodromes": 1}, "icaos": ["EGLL"]}


def fake_eaip_airspace(html_dir, bg_dir, region, cycle, log=None):
    (bg_dir / "ENR21").mkdir(parents=True, exist_ok=True)
    (bg_dir / "ENR21" / "LONDON-TMA.draw").write_text("# LONDON TMA\n", encoding="utf-8")
    (bg_dir / "ENR21.lst").write_text("ENR21/LONDON-TMA.draw  DRAW  London TMA", encoding="utf-8")
    return {"enr_airspace": 1, "ad_airspace": 0}


def fake_openaip(cache_dir, log=None, progress_cb=None):
    return {
        "airports": {"LFPG": {"lat": 49.0097, "lon": 2.5479, "name": "Paris CDG", "country": "FR"},
                     "LFPO": {"lat": 48.7233, "lon": 2.3794, "name": "Paris Orly", "country": "FR"}},
        "navaids": {("CDG", "FR", 49.0, 2.5): {"ident": "CDG", "lat": 49.0, "lon": 2.5, "name": "CDG",
                                                "type": 2, "freq": "112.15", "elev_ft": 0, "region": "FR"}},
        "vfr_points": {("GB", "M25 JUNCTION 10", 51.3, -0.4): (51.3, -0.4)},
        "airspaces": [
            {"name": "LONDON CTR", "upper": "2500 ft", "lower": "GND", "rings": [SQUARE], "country": "GB"},
            {"name": "PARIS CTR", "upper": "FL65", "lower": "GND", "rings": [PARIS], "country": "FR"},
        ],
        "counts": {"base_openaip_airports": 2},
    }


def fake_ourairports(cache_dir, log=None):
    return {"airports": {"EGLL": {"lat": 51.4775, "lon": -0.4614, "name": "Heathrow", "country": "GB"},
                         "KJFK": {"lat": 40.64, "lon": -73.78, "name": "Kennedy", "country": "US"}},
            "counts": {"base_ourairports_airports": 2}}


@pytest.fixture
def offline(tmp_path, monkeypatch):
    html = tmp_path / "dist" / "airac" / "html"
    html.mkdir(parents=True)
    monkeypatch.setattr(make_airac.aip, "download_and_extract_airac",
                        lambda dest, log=None, progress_cb=None: ("11/26", html))
    monkeypatch.setattr(make_airac, "build_navdata_from_html", fake_nav_build)
    monkeypatch.setattr(make_airac, "build_airspace_from_html", fake_eaip_airspace)
    monkeypatch.setattr(make_airac.base, "load_openaip", fake_openaip)
    monkeypatch.setattr(make_airac.base, "load_ourairports", fake_ourairports)
    monkeypatch.setattr(make_airac.aip, "download_all_ad_layouts",
                        lambda icaos, ad_dir, log=None, skip=None, progress_cb=None:
                        {"downloaded": 0, "custom": 0, "unavailable": 0, "failed": 0, "layouts": []})
    monkeypatch.setattr(make_airac, "OVERRIDES_DIR", tmp_path / "no-overrides")

    def no_upload(*args, **kwargs):
        pytest.fail("nothing may be uploaded")

    monkeypatch.setattr(make_airac, "publish", no_upload)
    monkeypatch.setattr(make_airac.subprocess, "run", no_upload)
    return tmp_path / "dist"


def read_zip(path):
    with zipfile.ZipFile(path) as archive:
        return ({name: archive.read(name).decode("utf-8").replace("\r\n", "\n")
                 for name in archive.namelist()},
                json.loads(archive.read("manifest.json")))


def bg_names(root):
    return sorted(path.name for path in (root / "CONFIG" / "bg").iterdir())


def test_a_default_build_stages_both_eaip_and_openaip_airspace(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--regions", "none",
                            "--keep-staging"]) == 0
    staged = bg_names(offline / ".staging-EG")
    assert "ENR21.lst" in staged
    assert "OAIPGB.lst" in staged and "OAIPFR.lst" in staged
    assert "OAIP-countries" in staged
    assert "OAIP.lst" not in staged


def test_a_plain_run_builds_everything_and_uploads_nothing(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--min-region-airports", "1"]) == 0
    files, manifest = read_zip(offline / "EG_2611.zip")
    assert files["CONFIG/nav/fix.dat"] == "51.000000 -1.000000 ABBEW\n"
    assert "CONFIG/bg/ENR21.lst" in files
    assert "CONFIG/bg/OAIPGB/LONDON-CTR.draw" in files
    assert "EG GB" in files["CONFIG/bg/OAIP-countries"]
    assert "M25 JUNCTION 10" in files["CONFIG/nav/vfr-points"]
    assert "CC BY-NC 4.0" in files["CONFIG/nav/NOTICE.txt"]
    idents = [line.split()[0] for line in files["CONFIG/nav/AD-positions"].splitlines()[:-1]]
    assert sorted(idents) == ["EGLL", "KJFK", "LFPG", "LFPO"]
    assert "CC BY-NC 4.0" in manifest["sources"]["openaip"]
    assert "NATS" in manifest["sources"]["airspace"] and "openAIP" in manifest["sources"]["airspace"]
    world, _ = read_zip(offline / "ALL_2611.zip")
    assert "CONFIG/bg/ENR21.lst" in world and "CONFIG/bg/OAIPFR.lst" in world
    region_files, _ = read_zip(offline / "LF_2611.zip")
    assert "CONFIG/bg/OAIPFR/PARIS-CTR.draw" in region_files
    assert "CONFIG/bg/OAIP-countries" in region_files
    assert "CONFIG/bg/ENR21.lst" not in region_files


def test_single_mode_names_the_asset_after_the_cycle(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--single"]) == 0
    assert sorted(path.name for path in offline.glob("*.zip")) == ["2611.zip"]
    files, manifest = read_zip(offline / "2611.zip")
    assert manifest["region"] == "EG"
    assert "CONFIG/bg/ENR21.lst" in files and "CONFIG/bg/OAIPGB.lst" in files


def test_eaip_alone_writes_no_openaip_groups_anywhere(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--regions", "LF",
                            "--airspace-source", "eaip"]) == 0
    files, manifest = read_zip(offline / "EG_2611.zip")
    assert "CONFIG/bg/ENR21.lst" in files
    assert not any(name.startswith("CONFIG/bg/OAIP") for name in files)
    assert manifest["sources"]["airspace"].startswith("NATS")
    region_files, _ = read_zip(offline / "LF_2611.zip")
    assert not any(name.startswith("CONFIG/bg/") for name in region_files)


def test_openaip_alone_writes_no_eaip_drawings(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--regions", "LF",
                            "--airspace-source", "openaip"]) == 0
    files, manifest = read_zip(offline / "EG_2611.zip")
    assert "CONFIG/bg/OAIPGB/LONDON-CTR.draw" in files
    assert "CONFIG/bg/ENR21.lst" not in files
    assert manifest["sources"]["airspace"].startswith("openAIP")
    region_files, _ = read_zip(offline / "LF_2611.zip")
    assert "CONFIG/bg/OAIPFR/PARIS-CTR.draw" in region_files


def test_without_the_base_there_is_no_world_or_region_zip(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--no-base"]) == 0
    assert sorted(path.name for path in offline.glob("*.zip")) == ["EG_2611.zip"]
    files, manifest = read_zip(offline / "EG_2611.zip")
    assert "CONFIG/nav/NOTICE.txt" not in files
    assert "CONFIG/bg/ENR21.lst" in files
    assert not any(name.startswith("CONFIG/bg/OAIP") for name in files)
    assert "openaip" not in manifest["sources"]
    assert manifest["sources"]["airspace"].startswith("NATS")


def test_an_override_of_a_merged_file_still_gets_the_base_rows(offline, tmp_path, monkeypatch):
    overrides = tmp_path / "CONFIG"
    (overrides / "nav").mkdir(parents=True)
    (overrides / "nav" / "navaid.dat").write_text(
        "2 55.000000 -3.000000 80 114.00 0 0 OVR 0 EG Override\n99\n", encoding="utf-8")
    monkeypatch.setattr(make_airac, "OVERRIDES_DIR", overrides)
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--regions", "none"]) == 0
    files, manifest = read_zip(offline / "EG_2611.zip")
    idents = [line.split()[7] for line in files["CONFIG/nav/navaid.dat"].splitlines()[:-1]]
    assert idents == ["OVR", "CDG"]
    assert manifest["overrides"] == ["CONFIG/nav/navaid.dat"]


def test_a_selective_run_reseeds_from_the_combined_zip_and_keeps_its_name(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--min-region-airports", "1"]) == 0
    before, _ = read_zip(offline / "LF_2611.zip")
    assert make_airac.main(["--out", str(offline), "--only", "airspace",
                            "--airspace-source", "openaip", "--airspace-scope", "eg"]) == 0
    files, manifest = read_zip(offline / "EG_2611.zip")
    assert manifest["region"] == "EG"
    assert "CONFIG/bg/OAIPGB/LONDON-CTR.draw" in files
    assert not any(name.startswith("CONFIG/bg/OAIPFR") for name in files)
    assert "CONFIG/bg/ENR21.lst" in files
    assert manifest["sources"]["regenerated"] == "airspace"
    assert read_zip(offline / "LF_2611.zip")[0] == before


def test_a_selective_airspace_run_rebuilds_both_by_default(offline):
    assert make_airac.main(["--out", str(offline), "--skip-layouts", "--regions", "none",
                            "--airspace-source", "eaip"]) == 0
    assert make_airac.main(["--out", str(offline), "--only", "airspace"]) == 0
    files, _ = read_zip(offline / "EG_2611.zip")
    assert "CONFIG/bg/ENR21.lst" in files
    assert "CONFIG/bg/OAIPGB.lst" in files and "CONFIG/bg/OAIPFR.lst" in files
    assert "CONFIG/bg/OAIP-countries" in files
