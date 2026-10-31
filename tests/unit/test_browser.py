"""Browser profile management, input checks and receipt validation. No browser is opened."""

import json
import stat
from pathlib import Path

import pytest

from zunara import browser

USER = "studio.clips"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_fresh_config_returns_full_profile_path_without_creating(tmp_path: Path):
    config = tmp_path / "config"
    expected = config / browser.PROFILE_ROOT / "instagram" / USER
    assert browser.profile_dir(config, "instagram", USER, create=False) == expected
    assert not config.exists()
    # Also when only the platform parent exists (e.g. created by the profile lock).
    with browser.profile_lock(config, "instagram", USER):
        assert browser.profile_dir(config, "instagram", USER, create=False) == expected
    assert not expected.exists()


def test_created_profile_is_private_and_marked(tmp_path: Path):
    config = tmp_path / "config"
    path = browser.profile_dir(config, "instagram", "@Studio.Clips", create=True)
    assert path == config / browser.PROFILE_ROOT / "instagram" / USER
    for directory in (path, path.parent, path.parent.parent):
        assert mode(directory) == 0o700
    marker = json.loads((path / browser.MARKER).read_text())
    assert marker["username"] == USER and "last_verified_login_at" not in marker
    assert not mode(path / browser.MARKER) & 0o077
    other = browser.profile_dir(config, "instagram", "other.account", create=True)
    assert other != path and other.parent == path.parent


@pytest.mark.parametrize("level", [0, 1, 2])
def test_symlinked_managed_paths_are_refused(tmp_path: Path, level: int):
    config = tmp_path / "config"
    parts = (browser.PROFILE_ROOT, "instagram", USER)
    parent = config.joinpath(*parts[:level])
    parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (parent / parts[level]).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        browser.profile_dir(config, "instagram", USER, create=True)


def test_foreign_profile_directory_is_refused(tmp_path: Path):
    config = tmp_path / "config"
    path = config / browser.PROFILE_ROOT / "instagram" / USER
    (path / "Default").mkdir(parents=True)
    with pytest.raises(ValueError, match="not created by Zunara"):
        browser.profile_dir(config, "instagram", USER, create=False)


def test_profile_nested_in_another_browser_is_refused(tmp_path: Path):
    chrome = tmp_path / "chrome-user-data"
    chrome.mkdir()
    (chrome / "Local State").write_text("{}")
    with pytest.raises(ValueError, match="another browser"):
        browser.profile_dir(chrome / "config", "instagram", USER, create=True)


def test_system_chrome_profile_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config" / "google-chrome" / "zunara"
    with pytest.raises(ValueError, match="personal browser profile"):
        browser.profile_dir(config, "instagram", USER, create=True)
    assert not config.exists()


def test_profile_lock_is_exclusive(tmp_path: Path):
    with (
        browser.profile_lock(tmp_path, "instagram", USER),
        pytest.raises(ValueError, match="in use"),
        browser.profile_lock(tmp_path, "instagram", USER),
    ):
        pass


@pytest.mark.parametrize(
    ("platform", "message"),
    [("tiktok", "not implemented for tiktok"), ("facebook", "Unknown platform")],
)
def test_only_instagram_is_supported(platform: str, message: str):
    with pytest.raises(ValueError, match=message):
        browser.check_platform(platform)


@pytest.mark.parametrize("name", ["", "has space", "x" * 31, "../escape"])
def test_usernames_are_validated(name: str):
    with pytest.raises(ValueError, match="username"):
        browser.check_username(name)


@pytest.mark.parametrize("value", [0, 29, 3601, float("nan")])
def test_timeouts_are_bounded(value: float):
    with pytest.raises(ValueError, match="between"):
        browser.check_timeout(value, "--timeout")


def test_doctor_is_offline_and_creates_nothing(tmp_path: Path):
    config = tmp_path / "config"
    report = browser.doctor(config)
    assert report["platforms"] == ["instagram"]
    assert report["profiles"] == [] and report["live_session"].startswith("unknown")
    assert not config.exists()
    browser.profile_dir(config, "instagram", USER, create=True)
    assert browser.doctor(config)["profiles"] == [
        {"platform": "instagram", "username": USER, "managed": True, "private": True}
    ]


def test_ledger_rejects_malformed_records_and_symlinks(tmp_path: Path):
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    path = work / "out" / browser.LEDGER
    record = {
        "platform": "instagram",
        "username": USER,
        "approved_sha256": "a" * 64,
        "file": "clip.mp4",
        "state": "published",
        "status": None,
        "media_selected": True,
        "share_clicked": True,
    }
    path.write_text(json.dumps({"version": 1, "records": [record]}))
    assert browser.prior_outcome(work, "a" * 64, USER) == "published"
    assert browser.prior_outcome(work, "a" * 64, "other") is None
    for bad in ({"state": "posted"}, {"platform": "tiktok"}, {"media_selected": "yes"}):
        path.write_text(json.dumps({"version": 1, "records": [{**record, **bad}]}))
        with pytest.raises(ValueError, match="invalid record"):
            browser.load_ledger(work)
    path.write_text(json.dumps({"version": 1, "records": [record, record]}))
    with pytest.raises(ValueError, match="duplicate"):
        browser.load_ledger(work)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(ValueError, match="symlink"):
        browser.load_ledger(work)


def test_unattached_stops_do_not_block_other_adapters(tmp_path: Path):
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    base = {
        "platform": "instagram",
        "username": USER,
        "file": "clip.mp4",
        "status": "login_required",
        "share_clicked": False,
    }
    records = [
        {**base, "approved_sha256": "a" * 64, "state": "stopped", "media_selected": False},
        {**base, "approved_sha256": "b" * 64, "state": "stopped", "media_selected": True},
    ]
    (work / "out" / browser.LEDGER).write_text(json.dumps({"version": 1, "records": records}))
    assert browser.prior_outcome(work, "a" * 64) is None
    assert browser.prior_outcome(work, "b" * 64) == "stopped"
