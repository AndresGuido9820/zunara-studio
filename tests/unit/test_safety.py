"""Regression tests for data loss and approval boundaries."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from zunara import queue
from zunara.config import Settings
from zunara.storage import media_path, workspace_lock, write_json


def approved(tmp_path):
    work = tmp_path / "session"
    (work / "out").mkdir(parents=True)
    (work / "out" / "a.mp4").write_bytes(b"approved bytes")
    rows = queue.refresh(work)
    rows[0]["title"] = "A"
    queue.save(work, rows)
    queue.approve(work, {"a"})
    return work


def test_empty_environment_does_not_store_tokens_in_current_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("ZUNARA_CONFIG_DIR", "ZUNARA_DATA_DIR", "ZUNARA_YOUTUBE_CLIENT_SECRET"):
        monkeypatch.setenv(key, "")
    settings = Settings.from_env()
    assert settings.config_dir != tmp_path
    assert settings.data_dir != tmp_path
    assert settings.client_secret == settings.config_dir / "client_secret.json"


def test_symlinked_output_is_rejected(tmp_path):
    work = tmp_path / "session"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.mp4").write_bytes(b"private")
    (work / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        media_path(work, "a.mp4")
    with pytest.raises(ValueError, match="symlink"):
        queue.refresh(work)


def test_empty_approval_cannot_approve_whole_queue(tmp_path):
    work = approved(tmp_path)
    with pytest.raises(ValueError, match="nonempty"):
        queue.approve(work, set())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "wrong"),
        ("approved_sha256", 1),
        ("youtube_id", 0),
        ("youtube_id", ""),
        ("upload_state", []),
        ("upload_state", "unknown"),
        ("upload_state", "uploaded"),
    ],
)
def test_state_fields_reject_malformed_values(tmp_path, field, value):
    work = approved(tmp_path)
    rows = queue.load(work)
    rows[0][field] = value
    queue.save(work, rows)
    with pytest.raises(ValueError):
        queue.load(work)


def test_export_uses_approved_hash_not_editable_cached_hash(tmp_path):
    work = approved(tmp_path)
    rows = queue.load(work)
    rows[0]["sha256"] = "0" * 64
    queue.save(work, rows)
    queue.export(work, tmp_path / "ready", "x", None)
    target = tmp_path / "ready" / "x" / "session"
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest[0]["sha256"] == hashlib.sha256((target / "a.mp4").read_bytes()).hexdigest()


def test_existing_export_is_never_mixed_with_new_batch(tmp_path):
    work = approved(tmp_path)
    destination = tmp_path / "ready"
    queue.export(work, destination, "x", None)
    target = destination / "x" / "session"
    (target / "old.mp4").write_bytes(b"old")
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        queue.export(work, destination, "x", None)
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


def test_copy_failure_leaves_no_partial_export(tmp_path, monkeypatch):
    work = approved(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfile", fail)
    with pytest.raises(OSError):
        queue.export(work, tmp_path / "ready", "x", None)
    assert not (tmp_path / "ready" / "x" / "session").exists()
    assert not list((tmp_path / "ready" / "x").glob(".zunara-export-*"))


def test_changed_copy_is_rejected_before_export_becomes_visible(tmp_path, monkeypatch):
    work = approved(tmp_path)

    def swapped(source, destination):
        Path(destination).write_bytes(b"new bytes")

    monkeypatch.setattr(shutil, "copyfile", swapped)
    with pytest.raises(ValueError, match="changed while copying"):
        queue.export(work, tmp_path / "ready", "x", None)
    assert not (tmp_path / "ready" / "x" / "session").exists()


def test_nested_workspace_lock_is_reentrant(tmp_path):
    work = approved(tmp_path)
    with workspace_lock(work), workspace_lock(work):
        assert queue.refresh(work)[0]["title"] == "A"


def test_private_directory_symlink_is_not_chmodded(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    alias = tmp_path / "tokens"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_json(alias / "main.json", {"fake": "test"}, private=True)
    assert target.stat().st_mode & 0o777 == 0o755
    assert not list(target.iterdir())
