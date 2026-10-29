import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from zunara import queue


def read_queue(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "out" / "queue.json").read_text(encoding="utf-8"))


def titled(work: Path, **titles: str) -> None:
    rows = queue.refresh(work)
    for row in rows:
        stem = Path(row["file"]).stem
        if stem in titles:
            row["title"] = titles[stem]
    queue.save(work, rows)


# load


def test_load_without_queue_file_is_empty(work: Path) -> None:
    assert queue.load(work) == []


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ({"file": "a.mp4"}, "array"),
        (["a.mp4"], "object"),
        ([{"file": "a.mp4"}, {"file": "a.mp4"}], "Duplicate"),
        ([{"file": "a.mp4", "title": 5}], "title"),
        ([{"file": "a.mp4", "description": None}], "description"),
        ([{"file": "a.mp4", "tags": "goal"}], "tags"),
        ([{"file": "a.mp4", "tags": ["ok", 1]}], "tags"),
        ([{"title": "no file"}], "plain .mp4"),
        ([{"file": ["a.mp4"]}], "plain .mp4"),
        ([{"file": "../a.mp4"}], "plain .mp4"),
        ([{"file": "missing.mp4"}], "Missing media"),
    ],
)
def test_load_rejects_malformed_queue(
    work: Path,
    add_clip: Callable[..., Path],
    write_queue: Callable[[object], Path],
    rows: object,
    message: str,
) -> None:
    add_clip("a.mp4")
    write_queue(rows)

    with pytest.raises(ValueError, match=message):
        queue.load(work)


def test_load_rejects_invalid_json(work: Path) -> None:
    (work / "out" / "queue.json").write_text("[{", encoding="utf-8")

    with pytest.raises(ValueError):
        queue.load(work)


def test_load_rejects_entry_pointing_outside_workspace(
    work: Path, tmp_path: Path, write_queue: Callable[[object], Path]
) -> None:
    (tmp_path / "other.mp4").write_bytes(b"x")
    (work / "out" / "other.mp4").symlink_to(tmp_path / "other.mp4")
    write_queue([{"file": "other.mp4"}])

    with pytest.raises(ValueError, match="inside the workspace"):
        queue.load(work)


# refresh


def test_refresh_requires_out_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="out directory"):
        queue.refresh(tmp_path)


def test_refresh_adds_new_clips_as_pending(work: Path, add_clip: Callable[..., Path]) -> None:
    add_clip("b.mp4", b"bbb")
    add_clip("a.mp4", b"aaa")
    (work / "out" / "notes.txt").write_text("ignored", encoding="utf-8")

    rows = queue.refresh(work)

    assert [r["file"] for r in rows] == ["a.mp4", "b.mp4"]
    assert rows[0] == {
        "file": "a.mp4",
        "title": "",
        "description": "",
        "tags": [],
        "sha256": hashlib.sha256(b"aaa").hexdigest(),
        "approved_sha256": None,
        "youtube_id": None,
        "upload_state": "pending",
    }
    assert read_queue(work) == rows


def test_refresh_keeps_metadata_and_approval_for_unchanged_media(
    work: Path, add_clip: Callable[..., Path]
) -> None:
    add_clip("a.mp4")
    titled(work, a="Goal")
    queue.approve(work, {"a"})

    rows = queue.refresh(work)

    assert rows[0]["title"] == "Goal"
    assert rows[0]["approved_sha256"] == rows[0]["sha256"]


def test_refresh_skips_symlinked_clip_outside_workspace(work: Path, tmp_path: Path) -> None:
    (tmp_path / "private.mp4").write_bytes(b"x")
    (work / "out" / "leak.mp4").symlink_to(tmp_path / "private.mp4")

    with pytest.raises(ValueError, match="inside the workspace"):
        queue.refresh(work)
    assert not (work / "out" / "queue.json").exists()


# media mutations and approval


def test_changed_media_clears_approval(work: Path, add_clip: Callable[..., Path]) -> None:
    clip = add_clip("a.mp4", b"v1")
    titled(work, a="Goal")
    queue.approve(work, {"a"})

    clip.write_bytes(b"v2")
    rows = queue.refresh(work)

    assert rows[0]["sha256"] == hashlib.sha256(b"v2").hexdigest()
    assert rows[0]["approved_sha256"] is None
    assert read_queue(work)[0]["approved_sha256"] is None


def test_verify_rejects_media_changed_after_approval_without_refresh(
    work: Path, add_clip: Callable[..., Path]
) -> None:
    clip = add_clip("a.mp4", b"v1")
    titled(work, a="Goal")
    queue.approve(work, {"a"})
    clip.write_bytes(b"v2")

    with pytest.raises(ValueError, match="changed after approval"):
        queue.verify_approved(work, queue.load(work)[0])


@pytest.mark.parametrize("state", [{"youtube_id": "abc123"}, {"upload_state": "uploading"}])
def test_refresh_refuses_mutation_of_uploaded_or_in_flight_media(
    work: Path, add_clip: Callable[..., Path], state: dict[str, str]
) -> None:
    clip = add_clip("a.mp4", b"v1")
    titled(work, a="Goal")
    queue.approve(work, {"a"})
    rows = queue.load(work)
    rows[0].update(state)
    queue.save(work, rows)
    before = read_queue(work)

    clip.write_bytes(b"v2")
    with pytest.raises(ValueError, match="new filename"):
        queue.refresh(work)

    assert read_queue(work) == before
    with pytest.raises(ValueError, match="changed after approval"):
        queue.verify_approved(work, queue.load(work)[0])


# select and approve


def test_select_without_names_returns_all() -> None:
    rows = [{"file": "a.mp4"}, {"file": "b.mp4"}]

    assert queue.select(rows, None) == rows
    with pytest.raises(ValueError, match="Selection must not be empty"):
        queue.select(rows, set())


def test_select_matches_by_stem() -> None:
    rows = [{"file": "a.mp4"}, {"file": "b.mp4"}]

    assert queue.select(rows, {"b"}) == [{"file": "b.mp4"}]


@pytest.mark.parametrize("names", [{"zzz"}, {"a", "zzz"}, {"a.mp4"}])
def test_select_rejects_unknown_names(names: set[str]) -> None:
    with pytest.raises(ValueError, match="Unknown clip names"):
        queue.select([{"file": "a.mp4"}], names)


def test_unknown_selector_lists_every_missing_name() -> None:
    with pytest.raises(ValueError, match="Unknown clip names: x, y"):
        queue.select([{"file": "a.mp4"}], {"y", "x"})


def test_approve_records_current_hash_for_selected_only(
    work: Path, add_clip: Callable[..., Path]
) -> None:
    add_clip("a.mp4", b"a")
    add_clip("b.mp4", b"b")
    titled(work, a="A", b="B")

    chosen = queue.approve(work, {"a"})

    assert [r["file"] for r in chosen] == ["a.mp4"]
    saved = {r["file"]: r for r in read_queue(work)}
    assert saved["a.mp4"]["approved_sha256"] == hashlib.sha256(b"a").hexdigest()
    assert saved["b.mp4"]["approved_sha256"] is None


@pytest.mark.parametrize("title", ["", "   "])
def test_approve_requires_title(work: Path, add_clip: Callable[..., Path], title: str) -> None:
    add_clip("a.mp4")
    titled(work, a=title)

    with pytest.raises(ValueError, match="Set a title"):
        queue.approve(work, {"a"})
    assert read_queue(work)[0]["approved_sha256"] is None


def test_approve_unknown_name_changes_nothing(work: Path, add_clip: Callable[..., Path]) -> None:
    add_clip("a.mp4")
    titled(work, a="A")

    with pytest.raises(ValueError, match="Unknown clip names"):
        queue.approve(work, {"a", "typo"})
    assert read_queue(work)[0]["approved_sha256"] is None


# verify_approved


def test_verify_rejects_unapproved(work: Path, add_clip: Callable[..., Path]) -> None:
    add_clip("a.mp4")
    titled(work, a="Goal")

    with pytest.raises(ValueError, match="needs review"):
        queue.verify_approved(work, queue.load(work)[0])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", "x" * 101, "1–100"),
        ("title", "  ", "1–100"),
        ("description", "d" * 5001, "5000"),
    ],
)
def test_verify_enforces_metadata_limits(
    work: Path, add_clip: Callable[..., Path], field: str, value: str, message: str
) -> None:
    add_clip("a.mp4")
    titled(work, a="Goal")
    row = queue.approve(work, {"a"})[0]
    row[field] = value

    with pytest.raises(ValueError, match=message):
        queue.verify_approved(work, row)


def test_verify_accepts_boundary_lengths(work: Path, add_clip: Callable[..., Path]) -> None:
    clip = add_clip("a.mp4")
    titled(work, a="t" * 100)
    row = queue.approve(work, {"a"})[0]
    row["description"] = "d" * 5000

    assert queue.verify_approved(work, row) == clip.resolve()


# export


@pytest.fixture
def approved(work: Path, add_clip: Callable[..., Path]) -> Path:
    add_clip("a.mp4", b"aaa")
    add_clip("b.mp4", b"bbb")
    rows = queue.refresh(work)
    rows[0].update(title="Golazo ⚽", description="Second half.\n", tags=["futbol"])
    rows[1].update(title="Save")
    queue.save(work, rows)
    queue.approve(work, {"a", "b"})
    return work


def test_export_copies_media_captions_and_manifest(approved: Path, tmp_path: Path) -> None:
    dest = tmp_path / "exports"

    count = queue.export(approved, dest, "tiktok", None)

    target = dest / "tiktok" / "session-01"
    assert count == 2
    assert (target / "a.mp4").read_bytes() == b"aaa"
    assert (target / "a.txt").read_text(encoding="utf-8") == "Golazo ⚽\n\nSecond half.\n"
    assert (target / "b.txt").read_text(encoding="utf-8") == "Save\n"
    assert json.loads((target / "manifest.json").read_text(encoding="utf-8")) == [
        {"file": "a.mp4", "sha256": hashlib.sha256(b"aaa").hexdigest()},
        {"file": "b.mp4", "sha256": hashlib.sha256(b"bbb").hexdigest()},
    ]


def test_export_manifest_hashes_match_exported_files(approved: Path, tmp_path: Path) -> None:
    queue.export(approved, tmp_path / "exports", "x", None)
    target = tmp_path / "exports" / "x" / "session-01"

    for entry in json.loads((target / "manifest.json").read_text(encoding="utf-8")):
        assert hashlib.sha256((target / entry["file"]).read_bytes()).hexdigest() == entry["sha256"]


def test_export_selected_clip_only(approved: Path, tmp_path: Path) -> None:
    assert queue.export(approved, tmp_path / "exports", "instagram", {"b"}) == 1

    target = tmp_path / "exports" / "instagram" / "session-01"
    assert sorted(p.name for p in target.iterdir()) == ["b.mp4", "b.txt", "manifest.json"]


def test_export_rejects_unknown_platform(approved: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported export platform"):
        queue.export(approved, tmp_path / "exports", "../facebook", None)
    assert not (tmp_path / "exports").exists()


def test_export_rejects_unknown_selector(approved: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown clip names"):
        queue.export(approved, tmp_path / "exports", "tiktok", {"nope"})
    assert not (tmp_path / "exports").exists()


def test_export_preflights_whole_batch_before_copying(approved: Path, tmp_path: Path) -> None:
    (approved / "out" / "b.mp4").write_bytes(b"edited after approval")

    with pytest.raises(ValueError, match="changed after approval"):
        queue.export(approved, tmp_path / "exports", "tiktok", None)
    assert not (tmp_path / "exports").exists()


def test_export_rejects_unsafe_workspace_name(tmp_path: Path) -> None:
    bad = tmp_path / "my session"
    (bad / "out").mkdir(parents=True)
    (bad / "out" / "a.mp4").write_bytes(b"a")
    rows = queue.refresh(bad)
    rows[0]["title"] = "A"
    queue.save(bad, rows)
    queue.approve(bad, {"a"})

    with pytest.raises(ValueError, match="names"):
        queue.export(bad, tmp_path / "exports", "tiktok", None)


def test_export_from_relative_current_workspace(
    approved: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(approved)

    assert queue.export(Path("."), tmp_path / "exports", "tiktok", None) == 2
