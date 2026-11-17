import os

from core.reportdir import resolve_report_dir


def test_report_dir_rules(tmp_path):
    cfg = tmp_path / "config"
    media = tmp_path / "media"
    cfg.mkdir()
    media.mkdir()
    allowed = lambda p: p.startswith(os.path.realpath(media))  # noqa: E731
    r = lambda v: resolve_report_dir(v, str(cfg), allowed)  # noqa: E731

    assert r("") == r(None) == r("techmark_reports") == (cfg / "techmark_reports").resolve()  # default, inside config
    assert r("a/b") == (cfg / "a" / "b").resolve()  # relative = inside config
    assert r(str(media / "reports")) == (media / "reports").resolve()  # absolute, allowlisted
    assert r(str(tmp_path / "elsewhere")) is None  # absolute, not allowlisted
    assert r("../elsewhere") is None  # traversal out of config
    assert r("a/../../elsewhere") is None


def test_media_source_uri_and_folder_list(tmp_path):
    from core.reportdir import list_media_folders

    cfg, media = tmp_path / "config", tmp_path / "media"
    (media / "novia_media" / "sub").mkdir(parents=True)
    (media / ".hidden").mkdir()
    cfg.mkdir()
    dirs = {"local": str(media)}
    deny = lambda p: False  # noqa: E731
    uri = "media-source://media_source/local/novia_media/"
    assert resolve_report_dir(uri, str(cfg), deny, dirs) == (media / "novia_media").resolve()
    assert resolve_report_dir("media-source://media_source/local/new_folder", str(cfg), deny, dirs) == (media / "new_folder").resolve()
    assert resolve_report_dir("media-source://media_source/local/../../x", str(cfg), deny, dirs) is None  # traversal
    assert resolve_report_dir("media-source://media_source/other/x", str(cfg), deny, dirs) is None  # unknown source
    assert resolve_report_dir(str(media / "novia_media"), str(cfg), deny, dirs) is not None  # absolute inside media dir
    assert resolve_report_dir(uri, str(cfg), deny, None) is None  # no media dirs known
    assert list_media_folders(dirs) == [
        "media-source://media_source/local/",
        "media-source://media_source/local/novia_media/",
        "media-source://media_source/local/novia_media/sub/",
    ]


def test_reports_are_written_read_only_and_can_be_regenerated(tmp_path):
    import stat

    from core.reportdir import write_readonly

    f = tmp_path / "attendance.csv"
    write_readonly(f, "first", "utf-8-sig", "")
    assert f.read_bytes() == b"\xef\xbb\xbffirst" and not f.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    write_readonly(f, "second")  # same name again: replaces the read-only file
    assert f.read_text() == "second" and not f.stat().st_mode & stat.S_IWUSR
    assert [p.name for p in tmp_path.iterdir()] == ["attendance.csv"]  # no temp file left behind
