import os
import time
import zipfile

import pytest

pytest.importorskip("bs4")

import build_navdata  # noqa: E402
import make_airac  # noqa: E402
import make_full  # noqa: E402
from skyhigh_navdata import aip  # noqa: E402


def test_publishing_is_off_unless_asked_for():
    assert make_airac.parse_args([]).publish is False
    assert make_airac.parse_args(["--only", "airways"]).publish is False
    assert make_airac.parse_args(["--no-publish"]).publish is False
    assert make_airac.parse_args(["--publish"]).publish is True


def test_clobber_is_its_own_flag_and_needs_publish():
    args = make_airac.parse_args(["--publish"])
    assert args.clobber is False
    assert make_airac.parse_args(["--publish", "--clobber"]).clobber is True
    with pytest.raises(SystemExit):
        make_airac.parse_args(["--clobber"])


def test_publish_and_no_publish_together_are_refused():
    with pytest.raises(SystemExit):
        make_airac.parse_args(["--publish", "--no-publish"])


@pytest.mark.parametrize("answer", ["", "n", "no", "nah", "x", "yse", "publish", "  "])
def test_only_a_clear_yes_publishes_from_the_menu(answer):
    assert make_full.publish_args(answer) == []


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_yes_publishes_from_the_menu(answer):
    assert make_full.publish_args(answer) == ["--publish"]


def test_make_full_passes_arguments_through_untouched():
    assert make_full.resolve_argv(["--only", "airways"]) == ["--only", "airways"]
    assert "--single" not in make_full.resolve_argv(["--regions", "none"])


def fake_gh(monkeypatch, existing):
    calls = []

    def run(command, capture_output=True, text=True):
        calls.append(command)

        class Result:
            returncode = 0
            stdout = "\n".join(existing or [])
            stderr = ""

        if command[1:3] == ["release", "view"] and existing is None:
            Result.returncode = 1
            Result.stderr = "release not found"
        return Result()

    monkeypatch.setattr(make_airac.subprocess, "run", run)
    return calls


def release_zip(tmp_path, name):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", '{"region": "EG", "files": []}')
    return path


def test_an_asset_already_on_the_release_is_not_replaced_without_clobber(tmp_path, monkeypatch):
    calls = fake_gh(monkeypatch, ["EG_2611.zip"])
    with pytest.raises(RuntimeError, match="--clobber"):
        make_airac.publish([release_zip(tmp_path, "EG_2611.zip")], "2611", "11/26", lambda m: None)
    assert not any("upload" in command for command in calls)


def test_clobber_is_only_passed_to_gh_when_asked_for(tmp_path, monkeypatch):
    calls = fake_gh(monkeypatch, ["ALL_2611.zip"])
    make_airac.publish([release_zip(tmp_path, "EG_2611.zip")], "2611", "11/26", lambda m: None)
    assert not any("--clobber" in command for command in calls)

    calls = fake_gh(monkeypatch, ["EG_2611.zip"])
    make_airac.publish([release_zip(tmp_path, "EG_2611.zip")], "2611", "11/26",
                       lambda m: None, clobber=True)
    assert any("upload" in command and "--clobber" in command for command in calls)


def test_a_partial_publish_leaves_the_release_notes_alone(tmp_path, monkeypatch):
    calls = fake_gh(monkeypatch, ["ALL_2611.zip", "LF_2611.zip"])
    make_airac.publish([release_zip(tmp_path, "EG_2611.zip")], "2611", "11/26",
                       lambda m: None, update_notes=False)
    assert not any("edit" in command for command in calls)


def test_a_failed_release_lookup_is_not_read_as_create(tmp_path, monkeypatch):
    def run(command, capture_output=True, text=True):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "error connecting to api.github.com"
        return Result()

    monkeypatch.setattr(make_airac.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        make_airac.publish([release_zip(tmp_path, "EG_2611.zip")], "2611", "11/26", lambda m: None)


def test_the_seed_is_the_combined_zip_not_the_newest_one(tmp_path):
    now = time.time()
    for age, name in enumerate(["LF_2611.zip", "ALL_2611.zip", "EG_2611.zip", "EG_2610.zip"]):
        path = tmp_path / name
        path.write_bytes(b"x" * 200_000)
        os.utime(path, (now - age * 60, now - age * 60))
    assert make_airac.combined_seed(tmp_path).name == "EG_2611.zip"
    (tmp_path / "2611.zip").write_bytes(b"x" * 200_000)
    assert make_airac.combined_seed(tmp_path).name == "2611.zip"
    assert make_airac.combined_seed(tmp_path / "missing") is None


def test_the_manifest_names_its_sources():
    sources = make_airac.build_sources({"airspaces": []}, "X-Plane Gateway", 1)
    assert "openAIP" in sources["openaip"] and "CC BY-NC 4.0" in sources["openaip"]
    assert "OurAirports" in sources["ourairports"]
    assert "NATS" in sources["aip"]
    assert make_airac.build_sources(None, "", 0).get("openaip") is None


def test_a_manifest_with_a_local_path_is_refused(tmp_path):
    (tmp_path / "CONFIG" / "nav").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="path from this machine"):
        make_airac.sealed_manifest(tmp_path, sources={"overrides": "C:\\builds\\CONFIG"})
    assert not (tmp_path / "manifest.json").exists()


def test_the_progress_line_keeps_its_timers_at_any_width():
    line = make_airac.render_line(
        0.5, "#" * 13 + "." * 13, "X-Plane Gateway airport layouts", "EGLL (1/200)",
        "01:00", "01:00", 79)
    assert len(line) == 79
    assert "01:00 elapsed | 01:00 left" in line


def test_skipped_steps_still_reach_one_hundred_percent():
    progress = make_airac.Progress()
    progress.plan([("a", 1), ("b", 3)])
    progress.skip("a")
    progress.skip("b")
    progress.finish("done")
    assert progress._fraction() == 1.0


def test_airspace_defaults_to_both_sources_at_world_scope():
    for module in (build_navdata, make_airac):
        args = module.parse_args([])
        assert args.airspace_source == "both"
        assert args.airspace_scope == "world"


def test_region_zips_follow_the_airspace_choice():
    data = {"airspaces": [{"name": "A"}]}
    assert build_navdata.region_airspaces(build_navdata.parse_args([]), data) == data["airspaces"]
    openaip = build_navdata.parse_args(["--airspace-source", "openaip"])
    assert build_navdata.region_airspaces(openaip, data) == data["airspaces"]
    eaip = build_navdata.parse_args(["--airspace-source", "eaip"])
    assert build_navdata.region_airspaces(eaip, data) is None
    skipped = build_navdata.parse_args(["--skip-airspace"])
    assert build_navdata.region_airspaces(skipped, data) is None


def test_both_without_base_data_falls_back_to_the_eaip_drawings():
    assert make_airac.airspace_plan("both", True) == (True, True)
    assert make_airac.airspace_plan("both", False) == (True, False)
    assert make_airac.airspace_plan("eaip", True) == (True, False)
    assert make_airac.airspace_plan("openaip", True) == (False, True)


def good_zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("index.html", "<html></html>")


def test_a_cached_archive_is_only_reused_for_the_url_it_came_from(tmp_path):
    archive = tmp_path / "nats-airac.zip"
    good_zip(archive)
    url = "https://example.invalid/AIRAC-10-26.zip"
    assert aip.cached_archive_usable(archive, url) is False
    aip.archive_source_path(archive).write_text(url + "\n", encoding="utf-8")
    assert aip.cached_archive_usable(archive, url) is True
    assert aip.cached_archive_usable(archive, "https://example.invalid/AIRAC-11-26.zip") is False


def test_a_truncated_archive_is_not_reused(tmp_path):
    archive = tmp_path / "nats-airac.zip"
    archive.write_bytes(b"PK\x03\x04 not a whole zip")
    url = "https://example.invalid/AIRAC-10-26.zip"
    aip.archive_source_path(archive).write_text(url, encoding="utf-8")
    assert aip.cached_archive_usable(archive, url) is False


class FakeResponse:
    def __init__(self, body, length=None):
        self._body = body
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, size=-1):
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_no_head_request_without_a_progress_callback(tmp_path, monkeypatch):
    methods = []

    def urlopen(request, timeout=None):
        methods.append(request.get_method())
        return FakeResponse(b"data")

    monkeypatch.setattr(aip, "urlopen", urlopen)
    aip.download_file("https://example.invalid/x", tmp_path / "x.bin")
    assert methods == ["GET"]
    assert (tmp_path / "x.bin").read_bytes() == b"data"


def test_a_short_download_leaves_no_file_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(aip, "urlopen", lambda request, timeout=None: FakeResponse(b"da", length=4))
    with pytest.raises(OSError):
        aip.download_file("https://example.invalid/x", tmp_path / "x.bin")
    assert list(tmp_path.iterdir()) == []


def test_openaip_alone_needs_the_base_data():
    with pytest.raises(SystemExit):
        make_airac.parse_args(["--no-base", "--airspace-source", "openaip"])
    assert make_airac.parse_args(["--no-base"]).airspace_source == "both"


def test_the_menu_never_adds_single_to_a_part_rebuild(monkeypatch):
    answers = iter(["2", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert make_full.main_menu() == ["--only", "airspace"]


@pytest.mark.parametrize("uploading, live", [("2611.zip", "EG_2611.zip"), ("EG_2611.zip", "2611.zip")])
def test_the_combined_navdata_under_its_other_name_is_a_conflict(tmp_path, monkeypatch, uploading, live):
    calls = fake_gh(monkeypatch, [live, "ALL_2611.zip"])
    with pytest.raises(RuntimeError, match=live.replace(".", r"\.")):
        make_airac.publish([release_zip(tmp_path, uploading)], "2611", "11/26", lambda m: None)
    assert not any("upload" in command or "delete-asset" in command for command in calls)


@pytest.mark.parametrize("uploading, live", [("2611.zip", "EG_2611.zip"), ("EG_2611.zip", "2611.zip")])
def test_clobber_deletes_the_other_combined_asset_after_the_upload(tmp_path, monkeypatch, uploading, live):
    calls = fake_gh(monkeypatch, [live, "ALL_2611.zip"])
    make_airac.publish([release_zip(tmp_path, uploading)], "2611", "11/26",
                       lambda m: None, clobber=True)
    verbs = [command[2] for command in calls if command[1] == "release"]
    assert verbs.index("upload") < verbs.index("delete-asset")
    deleted = [command for command in calls if "delete-asset" in command]
    assert len(deleted) == 1 and live in deleted[0] and "ALL_2611.zip" not in deleted[0]


def test_a_region_upload_does_not_touch_the_combined_asset(tmp_path, monkeypatch):
    calls = fake_gh(monkeypatch, ["EG_2611.zip", "2610.zip"])
    make_airac.publish([release_zip(tmp_path, "LF_2611.zip")], "2611", "11/26", lambda m: None)
    assert any("upload" in command for command in calls)
    assert not any("delete-asset" in command for command in calls)


def test_both_combined_names_are_never_uploaded_together(tmp_path, monkeypatch):
    calls = fake_gh(monkeypatch, [])
    with pytest.raises(RuntimeError, match="one name only"):
        make_airac.publish([release_zip(tmp_path, "EG_2611.zip"), release_zip(tmp_path, "2611.zip")],
                           "2611", "11/26", lambda m: None, clobber=True)
    assert calls == []


def test_single_mode_is_still_there_when_asked_for():
    assert make_airac.parse_args(["--single"]).single is True
    assert make_airac.parse_args([]).single is False
