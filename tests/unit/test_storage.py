import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from zunara.config import Settings
from zunara.storage import media_path, write_json

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_json_round_trips_unicode(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "state.json"

    write_json(target, {"title": "Golazo ⚽", "tags": ["a"]})

    text = target.read_text(encoding="utf-8")
    assert "Golazo ⚽" in text
    assert text.endswith("\n")
    assert json.loads(text) == {"title": "Golazo ⚽", "tags": ["a"]}


def test_write_json_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    write_json(target, [1])
    write_json(target, [2])

    assert json.loads(target.read_text(encoding="utf-8")) == [2]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


@pytest.mark.parametrize("bad", [float("nan"), {"x": float("inf")}, {"x": object()}])
def test_failed_write_keeps_previous_content_and_no_temp_files(tmp_path: Path, bad: object) -> None:
    target = tmp_path / "state.json"
    write_json(target, {"ok": True})

    with pytest.raises((ValueError, TypeError)):
        write_json(target, bad)

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_write_json_replaces_symlink_instead_of_following_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('"untouched"', encoding="utf-8")
    link = tmp_path / "work" / "queue.json"
    link.parent.mkdir()
    link.symlink_to(outside)

    write_json(link, ["new"])

    assert not link.is_symlink()
    assert json.loads(link.read_text(encoding="utf-8")) == ["new"]
    assert outside.read_text(encoding="utf-8") == '"untouched"'


@posix_only
def test_private_token_file_and_directory_are_owner_only(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "cfg", tmp_path / "data", tmp_path / "secret.json")
    token = settings.token_path("main")

    write_json(token, {"refresh_token": "dummy"}, private=True)

    assert mode(token) == 0o600
    assert mode(token.parent) == 0o700


@posix_only
def test_private_write_tightens_existing_directory(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir(mode=0o755)
    tokens.chmod(0o755)

    write_json(tokens / "main.json", {}, private=True)

    assert mode(tokens) == 0o700
    assert mode(tokens / "main.json") == 0o600


@posix_only
def test_private_overwrite_does_not_inherit_loose_permissions(tmp_path: Path) -> None:
    token = tmp_path / "tokens" / "main.json"
    token.parent.mkdir()
    token.write_text("{}", encoding="utf-8")
    token.chmod(0o644)

    write_json(token, {"refresh_token": "dummy"}, private=True)

    assert mode(token) == 0o600


def test_media_path_returns_resolved_file(work: Path, add_clip: Callable[..., Path]) -> None:
    clip = add_clip("goal.mp4")

    assert media_path(work, "goal.mp4") == clip.resolve()


@pytest.mark.parametrize(
    "name",
    ["../goal.mp4", "sub/goal.mp4", "/tmp/goal.mp4", "goal.mov", "goal.mp4.txt", "", None, 3],
)
def test_media_path_rejects_non_plain_names(work: Path, name: object) -> None:
    with pytest.raises(ValueError, match="plain .mp4 filename"):
        media_path(work, name)  # type: ignore[arg-type]


def test_media_path_rejects_missing_file(work: Path) -> None:
    with pytest.raises(ValueError, match="Missing media"):
        media_path(work, "ghost.mp4")


def test_media_path_rejects_directory_named_like_clip(work: Path) -> None:
    (work / "out" / "folder.mp4").mkdir()

    with pytest.raises(ValueError, match="Missing media"):
        media_path(work, "folder.mp4")


def test_media_path_rejects_symlink_escaping_out(work: Path, tmp_path: Path) -> None:
    secret = tmp_path / "private.mp4"
    secret.write_bytes(b"not yours")
    (work / "out" / "leak.mp4").symlink_to(secret)

    with pytest.raises(ValueError, match="inside the workspace"):
        media_path(work, "leak.mp4")


def test_media_path_rejects_symlink_into_subdirectory(work: Path) -> None:
    nested = work / "out" / "raw"
    nested.mkdir()
    (nested / "take.mp4").write_bytes(b"x")
    (work / "out" / "take.mp4").symlink_to(nested / "take.mp4")

    with pytest.raises(ValueError, match="inside the workspace"):
        media_path(work, "take.mp4")


def test_media_path_allows_symlink_within_out(work: Path, add_clip: Callable[..., Path]) -> None:
    real = add_clip("real.mp4")
    (work / "out" / "alias.mp4").symlink_to(real)

    assert media_path(work, "alias.mp4") == real.resolve()


def test_media_path_rejects_dangling_symlink(work: Path, tmp_path: Path) -> None:
    os.symlink(tmp_path / "gone.mp4", work / "out" / "gone.mp4")

    with pytest.raises(ValueError):
        media_path(work, "gone.mp4")
