"""tools/repackage_release.py and the seeded merge it shares with make_airac.py."""
import json
import zipfile

import pytest

pytest.importorskip("bs4")

import repackage_release  # noqa: E402
from skyhigh_navdata import base, merge, regions  # noqa: E402

PUBLISHED_DRAW = "# PARIS CTR\n# as published\nturquoise\n48.5000000,2.0000000\n48.5000000,3.0000000\n49.5000000,3.0000000\n"
PARIS = [(48.6, 2.1), (48.6, 2.9), (49.4, 2.9), (48.6, 2.1)]
ORLY = [(48.6, 2.2), (48.6, 2.5), (48.9, 2.5), (48.6, 2.2)]


BOTH = "%s, and %s" % (regions.EAIP_AIRSPACE, regions.OPENAIP_AIRSPACE)


def seed_zip(tmp_path, positions, counts=None, region="EG", overrides=None, airspace=None):
    path = tmp_path / "seed" / "EG_2611.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"region": region, "airac": "11/26", "airac_code": "2611",
                "counts": counts or {}, "overrides": overrides or [],
                "sources": {"aip": "NATS UK eAIP", "airspace": airspace or regions.EAIP_AIRSPACE}}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("CONFIG/nav/AD-positions", "\n".join(positions + [str(len(positions))]) + "\n")
        archive.writestr("CONFIG/nav/navaid.dat",
                         "2 51.487000 -0.467000 80 113.60 0 0 LON 0 EG London\n"
                         "2 49.000000 2.500000 300 112.15 0 0 CDG 0 FR Old name\n99\n")
        archive.writestr("CONFIG/nav/fix.dat", "51.000000 -1.000000 ABBEW\n")
        archive.writestr("CONFIG/bg/ENR21.lst", "ENR21/LONDON-TMA.draw  DRAW  London TMA")
        archive.writestr("CONFIG/bg/ENR21/LONDON-TMA.draw", "# LONDON TMA\n")
        archive.writestr("CONFIG/bg/OAIPFR.lst", "OAIPFR/PARIS-CTR.draw  DRAW  Paris CTR")
        archive.writestr("CONFIG/bg/OAIPFR/PARIS-CTR.draw", PUBLISHED_DRAW)
        archive.writestr("CONFIG/bg/OAIP-countries", "LF FR\n")
    return path


def fresh_sources(monkeypatch):
    openaip = {
        "airports": {"LFPG": {"lat": 49.0097, "lon": 2.5479, "name": "Paris CDG", "country": "FR"},
                     "KJFK": {"lat": 40.64, "lon": -73.78, "name": "Kennedy openAIP", "country": "US"}},
        "navaids": {("CDG", "FR", 49.0, 2.5): {"ident": "CDG", "lat": 49.0, "lon": 2.5, "name": "New name",
                                                "type": 2, "freq": "112.15", "elev_ft": 0, "region": "FR"}},
        "vfr_points": {},
        "airspaces": [
            {"name": "PARIS CTR", "upper": "FL65", "lower": "GND", "rings": [PARIS], "country": "FR"},
            {"name": "ORLY TMA", "upper": "FL115", "lower": "GND", "rings": [ORLY], "country": "FR"},
        ],
        "counts": {},
    }
    ourairports = {"airports": {"KJFK": {"lat": 40.64, "lon": -73.78, "name": "Kennedy OurAirports",
                                         "country": "US"},
                                "EGLL": {"lat": 51.4775, "lon": -0.4614, "name": "Heathrow", "country": "GB"}},
                   "counts": {}}
    monkeypatch.setattr(repackage_release.base, "load_openaip", lambda cache, log=None: openaip)
    monkeypatch.setattr(repackage_release.base, "load_ourairports", lambda cache, log=None: ourairports)


def read_zip(path):
    with zipfile.ZipFile(path) as archive:
        return ({name: archive.read(name).decode("utf-8").replace("\r\n", "\n")
                 for name in archive.namelist()},
                json.loads(archive.read("manifest.json")))


def positions_of(files):
    return {line.split()[0]: line.split(" ", 2)[2]
            for line in files["CONFIG/nav/AD-positions"].splitlines()[:-1]}


SEEDED = ["EGLL 51.477500,-0.461389 London Heathrow",
          "LFPG 49.009700,2.547900 Paris CDG",
          "KJFK 40.640000,-73.780000 Kennedy OurAirports"]


def run(tmp_path, seed, *extra):
    out = tmp_path / "out"
    code = repackage_release.main([str(seed), "--out", str(out), "--cache", str(tmp_path / "cache"),
                                   "--regions", "LF", *extra])
    return code, out


def test_a_seed_without_a_record_keeps_only_the_eaip_aerodromes(tmp_path, monkeypatch):
    fresh_sources(monkeypatch)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED))
    assert code == 0
    files, manifest = read_zip(out / "ALL_2611.zip")
    names = positions_of(files)
    assert names["EGLL"] == "London Heathrow"
    assert names["KJFK"] == "Kennedy openAIP"
    assert manifest["counts"]["base_rows_aerodromes"] == 2


def test_a_recorded_seed_drops_exactly_its_base_rows(tmp_path, monkeypatch):
    fresh_sources(monkeypatch)
    positions = ["EGLL 51.477500,-0.461389 London Heathrow",
                 "EIDW 53.421300,-6.270100 Dublin from the overrides",
                 "KJFK 40.640000,-73.780000 Kennedy OurAirports"]
    seed = seed_zip(tmp_path, positions, counts={"base_rows_aerodromes": 1, "base_rows_navaids": 1})
    code, out = run(tmp_path, seed)
    assert code == 0
    files, _ = read_zip(out / "ALL_2611.zip")
    names = positions_of(files)
    assert names["EIDW"] == "Dublin from the overrides"
    assert names["KJFK"] == "Kennedy openAIP"
    navaids = files["CONFIG/nav/navaid.dat"].splitlines()
    assert [row.split()[7] for row in navaids[:-1]] == ["LON", "CDG"]
    assert navaids[1].endswith("New name") and navaids[-1] == "99"


def test_keep_packages_the_published_drawings_in_the_regions(tmp_path, monkeypatch):
    fresh_sources(monkeypatch)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED))
    assert code == 0
    files, manifest = read_zip(out / "LF_2611.zip")
    assert files["CONFIG/bg/OAIPFR/PARIS-CTR.draw"] == PUBLISHED_DRAW
    assert "CONFIG/bg/OAIPFR/ORLY-TMA.draw" not in files
    assert files["CONFIG/bg/OAIPFR.lst"] == "OAIPFR/PARIS-CTR.draw  DRAW  Paris CTR"
    assert files["CONFIG/bg/OAIP-countries"] == "LF FR\n"
    assert manifest["counts"]["openaip_airspaces"] == 1
    world, world_manifest = read_zip(out / "ALL_2611.zip")
    assert world["CONFIG/bg/OAIPFR/PARIS-CTR.draw"] == PUBLISHED_DRAW
    assert world_manifest["sources"]["airspace"] == BOTH
    assert manifest["sources"]["airspace"] == regions.OPENAIP_AIRSPACE


def test_openaip_redraws_the_groups_and_keeps_the_eaip_drawings(tmp_path, monkeypatch):
    fresh_sources(monkeypatch)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED), "--airspace-source", "openaip")
    assert code == 0
    world, manifest = read_zip(out / "ALL_2611.zip")
    assert "CONFIG/bg/ENR21.lst" in world
    assert world["CONFIG/bg/OAIPFR/PARIS-CTR.draw"] != PUBLISHED_DRAW
    assert "openAIP" in manifest["sources"]["airspace"] and "NATS" in manifest["sources"]["airspace"]
    region_files, _ = read_zip(out / "LF_2611.zip")
    assert "CONFIG/bg/OAIPFR/ORLY-TMA.draw" in region_files


def test_a_failed_base_load_repackages_nothing(tmp_path, monkeypatch):
    fresh_sources(monkeypatch)

    def refuse(cache, log=None):
        raise base.BaseDataError("OurAirports could not be downloaded")

    monkeypatch.setattr(repackage_release.base, "load_ourairports", refuse)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED))
    assert code == 1
    assert list(out.iterdir()) == []


def test_a_region_zip_is_not_repackaged_as_the_combined_one(tmp_path, monkeypatch, capsys):
    fresh_sources(monkeypatch)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED, region="LF"))
    assert code == 1
    assert "LF package" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def staged(tmp_path, positions, navaids=("2 51.487000 -0.467000 80 113.60 0 0 LON 0 EG London",
                                         "2 49.000000 2.500000 300 112.15 0 0 CDG 0 FR CDG")):
    nav = tmp_path / "CONFIG" / "nav"
    nav.mkdir(parents=True)
    (nav / "AD-positions").write_text("\n".join(positions + [str(len(positions))]) + "\n", encoding="utf-8")
    (nav / "navaid.dat").write_text("\n".join(list(navaids) + ["99"]) + "\n", encoding="utf-8")
    return nav


def test_strip_uses_the_recorded_row_counts(tmp_path):
    nav = staged(tmp_path, SEEDED)
    removed = merge.strip_base_rows(
        tmp_path, {"counts": {"base_rows_aerodromes": 2, "base_rows_navaids": 1}})
    assert removed == {"aerodromes": 2, "navaids": 1}
    assert (nav / "AD-positions").read_text(encoding="utf-8") == SEEDED[0] + "\n1\n"
    assert (nav / "navaid.dat").read_text(encoding="utf-8").splitlines() == [
        "2 51.487000 -0.467000 80 113.60 0 0 LON 0 EG London", "99"]


def test_strip_does_not_trust_a_count_larger_than_the_file(tmp_path):
    nav = staged(tmp_path, SEEDED)
    merge.strip_base_rows(tmp_path, {"counts": {"base_rows_aerodromes": 99, "base_rows_navaids": 99}})
    assert (nav / "AD-positions").read_text(encoding="utf-8") == SEEDED[0] + "\n1\n"
    assert len((nav / "navaid.dat").read_text(encoding="utf-8").splitlines()) == 3


def test_strip_leaves_an_overridden_file_without_a_record_alone(tmp_path):
    nav = staged(tmp_path, SEEDED)
    before = (nav / "AD-positions").read_text(encoding="utf-8")
    merge.strip_base_rows(tmp_path, {"overrides": ["CONFIG/nav/AD-positions"]})
    assert (nav / "AD-positions").read_text(encoding="utf-8") == before


def test_the_merge_records_how_many_rows_it_appended(tmp_path):
    staged(tmp_path, SEEDED[:1])
    data = {"airports": {"LFPG": {"lat": 49.0, "lon": 2.5, "name": "Paris"}},
            "ourairports": {"airports": {"KJFK": {"lat": 40.6, "lon": -73.7, "name": "Kennedy"}}}}
    counts = merge.merge_base_into_stage(tmp_path, data)
    assert counts["base_rows_aerodromes"] == 2 and counts["base_rows_navaids"] == 0
    merge.strip_base_rows(tmp_path, {"counts": counts})
    assert merge.merge_base_into_stage(tmp_path, data)["base_rows_aerodromes"] == 2


def test_staged_drawings_are_copied_by_position_not_redrawn(tmp_path):
    stage_bg = tmp_path / "stage"
    (stage_bg / "OAIPFR").mkdir(parents=True)
    (stage_bg / "OAIPFR" / "PARIS-CTR.draw").write_text(PUBLISHED_DRAW, encoding="utf-8")
    (stage_bg / "OAIPFR" / "NICE-CTR.draw").write_text(
        "# NICE CTR\nturquoise\n43.5000000,7.0000000\n43.7000000,7.3000000\n", encoding="utf-8")
    (stage_bg / "OAIPFR.lst").write_text(
        "OAIPFR/PARIS-CTR.draw  DRAW  Paris CTR\nOAIPFR/NICE-CTR.draw  DRAW  Nice CTR\n"
        "../outside.draw  DRAW  Not ours", encoding="utf-8")
    (stage_bg / "ENR21.lst").write_text("ENR21/X.draw  DRAW  X", encoding="utf-8")
    region_bg = tmp_path / "region"
    assert regions.copy_staged_airspace_groups(stage_bg, region_bg, (47.0, 0.0, 51.0, 5.0)) == 1
    assert sorted(path.name for path in region_bg.rglob("*") if path.is_file()) == [
        "OAIPFR.lst", "PARIS-CTR.draw"]
    assert (region_bg / "OAIPFR" / "PARIS-CTR.draw").read_text(encoding="utf-8") == PUBLISHED_DRAW
    assert regions.copy_staged_airspace_groups(stage_bg, tmp_path / "far", (0.0, 100.0, 1.0, 101.0)) == 0
    assert not (tmp_path / "far").exists()


@pytest.mark.parametrize("seeded_label", [None, BOTH, "openAIP daily exports (CC BY-NC 4.0)", "skipped"])
def test_the_nats_credit_stays_while_eaip_drawings_are_in_the_package(tmp_path, monkeypatch, seeded_label):
    fresh_sources(monkeypatch)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED, airspace=seeded_label),
                    "--airspace-source", "openaip")
    assert code == 0
    world, manifest = read_zip(out / "ALL_2611.zip")
    assert "CONFIG/bg/ENR21.lst" in world and "CONFIG/bg/OAIPFR.lst" in world
    assert manifest["sources"]["airspace"] == BOTH


def test_the_airspace_label_names_what_the_tree_holds(tmp_path):
    assert regions.airspace_label(tmp_path / "missing") == "none in this package"
    (tmp_path / "OAIPFR.lst").write_text("x", encoding="utf-8")
    assert regions.airspace_label(tmp_path) == regions.OPENAIP_AIRSPACE
    (tmp_path / "EGLL.lst").write_text("x", encoding="utf-8")
    assert regions.airspace_label(tmp_path) == BOTH
    (tmp_path / "OAIPFR.lst").unlink()
    assert regions.airspace_label(tmp_path) == regions.EAIP_AIRSPACE


def no_private_path(text, tmp_path):
    for form in (str(tmp_path), tmp_path.as_posix(), str(tmp_path.parent), tmp_path.parent.as_posix()):
        assert form not in text
    return True


def test_a_failure_message_does_not_show_where_the_files_are(tmp_path, monkeypatch, capsys):
    fresh_sources(monkeypatch)
    cache_file = tmp_path / "cache" / "openaip" / "gb__apt.ndgeojson"

    def refuse(cache, log=None):
        raise PermissionError(13, "Permission denied", str(cache_file))

    monkeypatch.setattr(repackage_release.base, "load_openaip", refuse)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED))
    captured = capsys.readouterr()
    assert code == 1
    assert "Permission denied: gb__apt.ndgeojson" in captured.err
    assert no_private_path(captured.err + captured.out, tmp_path)


def test_a_missing_or_broken_seed_is_named_by_its_file_name_only(tmp_path, monkeypatch, capsys):
    fresh_sources(monkeypatch)
    assert run(tmp_path, tmp_path / "seed" / "EG_2611.zip")[0] == 1
    broken = tmp_path / "seed" / "EG_2612.zip"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a zip")
    assert run(tmp_path, broken)[0] == 1
    captured = capsys.readouterr()
    assert "EG_2611.zip was not found" in captured.err
    assert "EG_2612.zip is not a navdata ZIP" in captured.err
    assert no_private_path(captured.err + captured.out, tmp_path)


def test_a_failure_while_packaging_is_reported_without_the_path(tmp_path, monkeypatch, capsys):
    fresh_sources(monkeypatch)

    def refuse(stage, out_dir, *args, **kwargs):
        raise OSError(28, "No space left on device", str(out_dir / "ALL_2611.zip"))

    monkeypatch.setattr(repackage_release.regions, "build_global_zip", refuse)
    code, out = run(tmp_path, seed_zip(tmp_path, SEEDED))
    captured = capsys.readouterr()
    assert code == 1
    assert "No space left on device: ALL_2611.zip" in captured.err
    assert no_private_path(captured.err + captured.out, tmp_path)
    assert list(out.iterdir()) == []


def test_paths_in_messages_are_cut_to_the_file_name(tmp_path):
    from skyhigh_navdata import pack

    assert pack.scrub_paths("cannot open C:\\Users\\someone\\build\\EG_2611.zip now") == \
        "cannot open EG_2611.zip now"
    assert pack.scrub_paths("wrote /home/someone/dist/EG_2611.zip") == "wrote EG_2611.zip"
    assert pack.scrub_paths("see https://example.invalid/a/b.zip and dist/EG_2611.zip") == \
        "see https://example.invalid/a/b.zip and dist/EG_2611.zip"
    assert pack.scrub_paths("ENR 2.1 / AD 2.17, cycle 11/26") == "ENR 2.1 / AD 2.17, cycle 11/26"
    assert pack.display_path("dist/EG_2611.zip") == "dist/EG_2611.zip"
    assert pack.display_path(tmp_path / "x" / "EG_2611.zip") == "EG_2611.zip"
    assert pack.display_path(pack.REPO_ROOT / "CONFIG" / "nav") == "CONFIG/nav"
    error = FileNotFoundError(2, "No such file or directory", str(tmp_path / "gone.csv"))
    assert pack.error_text(error) == "No such file or directory: gone.csv"
    assert str(tmp_path) not in pack.error_text(RuntimeError("bad file %s" % (tmp_path / "a.zip")))
