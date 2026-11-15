import re

from skyhigh_navdata import merge

FIX_ROW = re.compile(r"^-?\d+\.\d+ -?\d+\.\d+ [A-Z0-9]+$")


def nav_dir(tmp_path):
    path = tmp_path / "CONFIG" / "nav"
    path.mkdir(parents=True)
    return path


def base_navaid(ident, lat, lon, region="DE", **extra):
    record = {"ident": ident, "lat": lat, "lon": lon, "region": region,
              "name": ident, "type": 2, "freq": "112.30", "elev_ft": 100}
    record.update(extra)
    return record


def test_fix_dat_is_left_as_the_eaip_wrote_it(tmp_path):
    nav = nav_dir(tmp_path)
    original = "51.000000 -1.000000 ABBEW\n52.000000 -2.000000 ADMIS\n"
    (nav / "fix.dat").write_text(original, encoding="utf-8")
    base = {"vfr_points": {("GB", "M25 JUNCTION 10", 51.3, -0.4): (51.3, -0.4),
                           ("FR", "N", 48.0, 2.0): (48.0, 2.0),
                           ("FR", "N", 45.0, 3.0): (45.0, 3.0)}}
    counts = merge.merge_base_into_stage(tmp_path, base)
    assert (nav / "fix.dat").read_text(encoding="utf-8") == original
    assert all(FIX_ROW.match(line) for line in original.splitlines())
    assert counts["base_vfr_points"] == 3


def test_vfr_points_go_to_their_own_tab_separated_file(tmp_path):
    nav = nav_dir(tmp_path)
    merge.write_vfr_points(nav, {"vfr_points": {
        ("GB", "M25  JUNCTION\t10", 51.3, -0.4): (51.3, -0.4)}})
    rows = [line for line in (nav / "vfr-points").read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")]
    assert rows == ["51.300000\t-0.400000\tGB\tM25 JUNCTION 10"]


def test_no_vfr_points_removes_a_file_from_an_earlier_run(tmp_path):
    nav = nav_dir(tmp_path)
    (nav / "vfr-points").write_text("old\n", encoding="utf-8")
    assert merge.write_vfr_points(nav, {}) == 0
    assert not (nav / "vfr-points").exists()


def test_a_navaid_without_an_elevation_is_written_as_zero():
    row = merge.navaid_row(base_navaid("ABC", 50.0, 10.0, elev_ft=None))
    assert row.split()[3] == "0"
    assert "None" not in row


def test_a_navaid_row_has_the_columns_the_readers_expect():
    parts = merge.navaid_row(base_navaid("ABC", 50.0, 10.0, name="Some  Place\tVOR")).split()
    assert parts[0] == "2" and parts[7] == "ABC" and parts[9] == "DE"
    assert parts[10:] == ["Some", "Place", "VOR"]


def test_a_uk_ident_does_not_hide_a_foreign_navaid(tmp_path):
    nav = nav_dir(tmp_path)
    (nav / "navaid.dat").write_text(
        "2 51.487000 -0.467000 80 113.60 0 0 LON 0 EG London\n99\n", encoding="utf-8")
    base = {"navaids": {
        ("LON", "GB", 51.5, -0.5): base_navaid("LON", 51.4871, -0.4669, region="GB"),
        ("LON", "US", 40.0, -100.0): base_navaid("LON", 40.0, -100.0, region="US"),
        ("LHR", "AU", -30.0, 150.0): base_navaid("LHR", -30.0, 150.0, region="AU"),
        ("LHR", "DE", 50.0, 10.0): base_navaid("LHR", 50.0, 10.0, region="DE"),
    }}
    assert merge.merge_navaids(nav, base) == 3
    lines = (nav / "navaid.dat").read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("LON 0 EG London")
    assert lines[-1] == "99"
    idents = [(line.split()[7], line.split()[9]) for line in lines[:-1]]
    assert idents.count(("LON", "EG")) == 1
    assert ("LON", "GB") not in idents
    assert ("LON", "US") in idents
    assert ("LHR", "AU") in idents and ("LHR", "DE") in idents


def test_the_merge_can_run_twice_without_adding_rows_twice(tmp_path):
    nav = nav_dir(tmp_path)
    (nav / "navaid.dat").write_text("2 51.0 -1.0 80 113.60 0 0 AAA 0 EG Aaa\n99\n", encoding="utf-8")
    (nav / "AD-positions").write_text("EGLL 51.477500,-0.461389 London Heathrow\n1\n", encoding="utf-8")
    base = {"navaids": {("BBB", "FR", 48.0, 2.0): base_navaid("BBB", 48.0, 2.0, region="FR")},
            "airports": {"LFPG": {"lat": 49.0, "lon": 2.5, "name": "Paris"}}}
    merge.merge_base_into_stage(tmp_path, base)
    first = {name: (nav / name).read_text(encoding="utf-8") for name in ("navaid.dat", "AD-positions")}
    merge.merge_base_into_stage(tmp_path, base)
    assert first == {name: (nav / name).read_text(encoding="utf-8") for name in first}


def test_only_icao_codes_are_added_to_ad_positions(tmp_path):
    nav = nav_dir(tmp_path)
    (nav / "AD-positions").write_text("EGLL 51.477500,-0.461389 London Heathrow\n1\n", encoding="utf-8")
    base = {
        "airports": {"LFPG": {"lat": 49.0, "lon": 2.5, "name": "Paris"},
                     "00AK": {"lat": 59.9, "lon": -151.7, "name": "Lowell"}},
        "ourairports": {"airports": {"KJFK": {"lat": 40.6, "lon": -73.8, "name": "Kennedy"},
                                     "01CA": {"lat": 34.0, "lon": -118.0, "name": "Pad"},
                                     "EGLL": {"lat": 0.0, "lon": 0.0, "name": "Wrong"}}},
    }
    assert merge.merge_aerodromes(nav, base) == 2
    lines = (nav / "AD-positions").read_text(encoding="utf-8").splitlines()
    assert [line.split()[0] for line in lines[:-1]] == ["EGLL", "LFPG", "KJFK"]
    assert lines[0].endswith("London Heathrow")
    assert lines[-1] == "3"


def test_the_notice_credits_openaip_and_ships_in_an_installable_folder(tmp_path):
    nav_dir(tmp_path)
    merge.merge_base_into_stage(tmp_path, {})
    notice = (tmp_path / "CONFIG" / "nav" / "NOTICE.txt").read_text(encoding="utf-8")
    assert "openAIP" in notice and "CC BY-NC 4.0" in notice and "OurAirports" in notice
