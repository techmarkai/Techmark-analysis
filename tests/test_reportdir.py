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
