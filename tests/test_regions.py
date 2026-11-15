import zipfile

import pytest

from skyhigh_navdata import pack, regions

SQUARE = [(50.0, -1.0), (50.0, 0.0), (51.0, 0.0), (50.0, -1.0)]
WORLD = regions.WORLD_BBOX


def test_the_country_map_reads_what_load_ourairports_returns(tmp_path):
    result = {
        "airports": {
            "EGLL": {"country": "GB"}, "EGKK": {"country": "GB"}, "EGJJ": {"country": "JE"},
            "LFPG": {"country": "FR"}, "KJFK": {"country": "US"},
            "00AK": {"country": "US"},
        },
        "counts": {"base_ourairports_airports": 6},
    }
    assert regions.write_icao_country_map(tmp_path, result) == 3
    lines = [line for line in (tmp_path / "OAIP-countries").read_text(encoding="utf-8").splitlines()
             if not line.startswith("#")]
    assert lines == ["EG GB", "K US", "LF FR"]


def test_the_country_map_also_takes_the_aerodrome_dict_itself(tmp_path):
    assert regions.write_icao_country_map(tmp_path, {"EGLL": {"country": "GB"}}) == 1


def test_no_aerodromes_writes_no_map(tmp_path):
    assert regions.write_icao_country_map(tmp_path, {"airports": {}, "counts": {}}) == 0
    assert not (tmp_path / "OAIP-countries").exists()


def test_local_idents_do_not_become_regions(tmp_path):
    nav = tmp_path / "CONFIG" / "nav"
    nav.mkdir(parents=True)
    (nav / "AD-positions").write_text(
        "LFPG 49.000000,2.500000 Paris\nLFPO 48.700000,2.300000 Orly\n"
        "00AK 59.900000,-151.700000 Lowell\n01CA 34.000000,-118.000000 Pad\n4\n",
        encoding="utf-8")
    assert regions.resolve_regions("auto", tmp_path, 1) == ["LF"]


def test_two_volumes_with_one_name_are_both_written(tmp_path):
    other = [(60.0, -1.0), (60.0, 0.0), (61.0, 0.0), (60.0, -1.0)]
    airspaces = [
        {"name": "DANGER AREA", "upper": "FL100", "lower": "GND", "rings": [SQUARE], "country": "GB"},
        {"name": "DANGER AREA", "upper": "FL200", "lower": "FL100", "rings": [other], "country": "GB"},
        {"name": "DANGER AREA", "upper": "FL100", "lower": "GND", "rings": [SQUARE], "country": "GB"},
    ]
    count, group = regions.write_openaip_airspace_group(tmp_path, airspaces, WORLD, "10/26")
    assert count == 2
    assert sorted(path.name for path in (tmp_path / group).glob("*.draw")) == [
        "DANGER-AREA-2.draw", "DANGER-AREA.draw"]
    assert "FL200" in (tmp_path / group / "DANGER-AREA-2.draw").read_text(encoding="utf-8")


def test_a_second_volume_does_not_overwrite_a_name_that_ends_in_its_number(tmp_path):
    airspaces = [
        {"name": "LONDON TMA 2", "upper": "FL100", "lower": "GND", "rings": [SQUARE]},
        {"name": "LONDON TMA", "upper": "FL100", "lower": "GND", "rings": [SQUARE]},
        {"name": "LONDON TMA", "upper": "FL200", "lower": "FL100", "rings": [SQUARE]},
    ]
    count, group = regions.write_openaip_airspace_group(tmp_path, airspaces, WORLD, "10/26")
    assert count == 3
    assert len(list((tmp_path / group).glob("*.draw"))) == 3


def test_clearing_removes_every_openaip_group_and_nothing_else(tmp_path):
    for name in ("OAIP", "OAIPGB", "OAIPFR", "ENR21", "EGLL"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.draw").write_text("x", encoding="utf-8")
        (tmp_path / (name + ".lst")).write_text("x", encoding="utf-8")
    (tmp_path / "OAIP-countries").write_text("EG GB\n", encoding="utf-8")
    (tmp_path / "Notice").write_text("keep", encoding="utf-8")
    regions.clear_openaip_groups(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "EGLL", "EGLL.lst", "ENR21", "ENR21.lst", "Notice"]


def test_the_world_groups_replace_the_flat_group(tmp_path):
    (tmp_path / "OAIP").mkdir()
    (tmp_path / "OAIP.lst").write_text("x", encoding="utf-8")
    (tmp_path / "OAIPDE").mkdir()
    (tmp_path / "OAIPDE.lst").write_text("stale", encoding="utf-8")
    airspaces = [{"name": "A", "upper": "", "lower": "", "rings": [SQUARE], "country": "GB"}]
    assert regions.write_world_airspace_groups(tmp_path, airspaces, "10/26") == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["OAIPGB", "OAIPGB.lst"]


def test_an_airway_ending_on_a_navaid_is_kept(tmp_path):
    source = tmp_path / "awy.dat"
    source.write_text(
        "ABBEW EG 11 LON EG 3 N 1 85 244 L6\n"
        "ABBEW EG 11 FARAW EG 11 N 1 85 244 L9\n", encoding="utf-8")
    target = tmp_path / "out" / "awy.dat"
    assert regions._filter_airways(source, target, {"ABBEW", "LON"}) == 1
    assert target.read_text(encoding="utf-8").splitlines() == ["ABBEW EG 11 LON EG 3 N 1 85 244 L6"]


def test_navaid_idents_are_read_from_the_file(tmp_path):
    path = tmp_path / "navaid.dat"
    path.write_text("2 51.487 -0.467 80 113.60 0 0 LON 0 EG London\n99\n", encoding="utf-8")
    assert regions._parse_navaid_idents(path) == {"LON"}


def test_safe_extract_refuses_a_member_outside_the_folder(tmp_path):
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("CONFIG/nav/fix.dat", "ok")
        archive.writestr("../escaped.txt", "no")
    with zipfile.ZipFile(archive_path) as archive, pytest.raises(ValueError):
        pack.safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "out").exists()


def test_safe_extract_writes_a_normal_archive(tmp_path):
    archive_path = tmp_path / "good.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("CONFIG/nav/fix.dat", "ok")
    with zipfile.ZipFile(archive_path) as archive:
        pack.safe_extract(archive, tmp_path / "out")
    assert (tmp_path / "out" / "CONFIG" / "nav" / "fix.dat").read_text() == "ok"


def staged_tree(tmp_path):
    nav = tmp_path / "stage" / "CONFIG" / "nav"
    nav.mkdir(parents=True)
    (nav / "AD-positions").write_text(
        "EGLL 51.477500,-0.461389 London Heathrow\n"
        "LFPG 49.009700,2.547900 Paris Charles de Gaulle\n"
        "LFPO 48.723300,2.379400 Paris Orly\n3\n", encoding="utf-8")
    (nav / "fix.dat").write_text("51.000000 -1.000000 ABBEW\n48.900000 2.400000 PARIS\n", encoding="utf-8")
    (nav / "navaid.dat").write_text(
        "2 51.487000 -0.467000 80 113.60 0 0 LON 0 EG London\n"
        "2 49.000000 2.500000 300 112.15 0 0 CDG 0 FR Charles de Gaulle\n99\n", encoding="utf-8")
    (nav / "awy.dat").write_text("PARIS LF 11 CDG LF 3 N 1 85 244 A1\n", encoding="utf-8")
    (nav / "vfr-points").write_text(
        "# header\n48.800000\t2.300000\tFR\tN\n51.300000\t-0.400000\tGB\tM25 JUNCTION 10\n",
        encoding="utf-8")
    (nav / "NOTICE.txt").write_text("credits\n", encoding="utf-8")
    return tmp_path / "stage"


def test_a_region_zip_carries_its_points_the_notice_and_the_sources(tmp_path):
    stage = staged_tree(tmp_path)
    sources = {"openaip": "openAIP daily exports, CC BY-NC 4.0"}
    zip_path = regions.build_region_zip(
        "LF", stage, tmp_path / "dist", "10/26", "2610", base_airspaces=None, sources=sources)
    assert zip_path.name == "LF_2610.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        manifest = __import__("json").loads(archive.read("manifest.json"))
        points = archive.read("CONFIG/nav/vfr-points").decode("utf-8")
        airways = archive.read("CONFIG/nav/awy.dat").decode("utf-8")
    assert "CONFIG/nav/NOTICE.txt" in names
    assert not any(name.startswith("CONFIG/bg/") for name in names)
    assert manifest["sources"]["openaip"] == sources["openaip"]
    assert manifest["sources"]["airspace"] == "none in this region package"
    assert "\tFR\tN" in points and "M25" not in points
    assert "CDG" in airways
    assert pack.verify_zip(zip_path) == []


def test_a_region_zip_draws_openaip_airspace_when_given_it(tmp_path):
    stage = staged_tree(tmp_path)
    paris = [(48.5, 2.0), (48.5, 3.0), (49.5, 3.0), (48.5, 2.0)]
    airspaces = [{"name": "PARIS CTR", "upper": "FL65", "lower": "GND", "rings": [paris], "country": "FR"}]
    zip_path = regions.build_region_zip(
        "LF", stage, tmp_path / "dist", "10/26", "2610", base_airspaces=airspaces)
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert "CONFIG/bg/OAIPFR/PARIS-CTR.draw" in names
    assert "CONFIG/bg/OAIPFR.lst" in names


def test_a_region_zip_carries_the_country_map_of_the_combined_build(tmp_path):
    stage = staged_tree(tmp_path)
    (stage / "CONFIG" / "bg").mkdir()
    (stage / "CONFIG" / "bg" / "OAIP-countries").write_text("LF FR\n", encoding="utf-8")
    paris = [(48.5, 2.0), (48.5, 3.0), (49.5, 3.0), (48.5, 2.0)]
    airspaces = [{"name": "PARIS CTR", "upper": "FL65", "lower": "GND", "rings": [paris], "country": "FR"}]
    zip_path = regions.build_region_zip(
        "LF", stage, tmp_path / "dist", "10/26", "2610", base_airspaces=airspaces)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.read("CONFIG/bg/OAIP-countries").decode("utf-8").split() == ["LF", "FR"]


def openaip_data():
    paris = [(48.5, 2.0), (48.5, 3.0), (49.5, 3.0), (48.5, 2.0)]
    return {
        "airspaces": [
            {"name": "LONDON CTR", "upper": "2500 ft", "lower": "GND", "rings": [SQUARE], "country": "GB"},
            {"name": "PARIS CTR", "upper": "FL65", "lower": "GND", "rings": [paris], "country": "FR"},
        ],
        "ourairports": {"airports": {"EGLL": {"country": "GB"}, "LFPG": {"country": "FR"}},
                        "counts": {}},
    }


def test_openaip_airspace_is_written_as_country_groups_with_the_map(tmp_path):
    (tmp_path / "ENR21").mkdir()
    (tmp_path / "ENR21.lst").write_text("x", encoding="utf-8")
    (tmp_path / "OAIP.lst").write_text("stale flat group", encoding="utf-8")
    assert regions.write_openaip_airspace(tmp_path, openaip_data(), "world", "10/26") == 2
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "ENR21", "ENR21.lst", "OAIP-countries", "OAIPFR", "OAIPFR.lst", "OAIPGB", "OAIPGB.lst"]
    assert "EG GB" in (tmp_path / "OAIP-countries").read_text(encoding="utf-8")


def test_the_eg_scope_keeps_only_the_uk_and_ireland(tmp_path):
    assert regions.write_openaip_airspace(tmp_path, openaip_data(), "eg", "10/26") == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "OAIP-countries", "OAIPGB", "OAIPGB.lst"]


def test_the_world_zip_refuses_a_local_path_in_its_sources(tmp_path):
    stage = staged_tree(tmp_path)
    with pytest.raises(RuntimeError, match="path from this machine"):
        regions.build_global_zip(stage, tmp_path / "dist", "10/26", "2610",
                                 sources={"overrides": "3 file(s) from /home/someone/CONFIG"})
