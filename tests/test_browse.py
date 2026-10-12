from __future__ import annotations

import os

import pytest

from shuto.web import browse


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(browse, "roots", lambda: [browse.Root("Home", str(root))])
    return root


def test_lists_dirs_only_with_photo_counts(home):
    (home / "trip").mkdir()
    (home / "trip" / "a.jpg").write_bytes(b"x")
    (home / "trip" / "b.PNG").write_bytes(b"x")
    (home / "trip" / "notes.txt").write_text("hi")
    (home / "empty").mkdir()
    (home / ".hidden").mkdir()
    (home / "file.jpg").write_bytes(b"x")
    listing = browse.list_dir(str(home))
    names = [d["name"] for d in listing.dirs]
    assert names == ["empty", "trip"]
    counts = {d["name"]: d["photo_count"] for d in listing.dirs}
    assert counts == {"empty": 0, "trip": 2}
    assert listing.parent is None
    assert listing.crumbs[0]["path"] == str(home.resolve())


def test_default_is_first_root_and_subfolder_has_parent_and_crumbs(home):
    (home / "a" / "b").mkdir(parents=True)
    assert browse.list_dir("").path == str(home.resolve())
    sub = browse.list_dir(str(home / "a" / "b"))
    assert sub.parent == str((home / "a").resolve())
    assert [c["name"] for c in sub.crumbs][-2:] == ["a", "b"]


def test_rejects_dotdot_outside_files_and_missing(home, tmp_path):
    (tmp_path / "other").mkdir()
    (home / "f.jpg").write_bytes(b"x")
    for bad in (
        str(home / ".." / "other"),
        str(tmp_path / "other"),
        str(home / "f.jpg"),
        str(home / "missing"),
        "bad\x00path",
    ):
        with pytest.raises(browse.BrowseError):
            browse.list_dir(bad)


def test_symlink_escape_is_hidden_and_refused(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert browse.list_dir(str(home)).dirs == []
    with pytest.raises(browse.BrowseError):
        browse.list_dir(str(link))


def test_truncates_long_listings(home, monkeypatch):
    monkeypatch.setattr(browse, "MAX_DIRS", 3)
    for i in range(5):
        (home / f"d{i}").mkdir()
    listing = browse.list_dir(str(home))
    assert len(listing.dirs) == 3
    assert listing.truncated


def test_real_roots_include_home():
    assert browse.roots()[0].path == str(__import__("pathlib").Path.home())
