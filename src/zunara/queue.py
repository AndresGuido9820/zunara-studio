"""Review queue: explicit approval, content hashes, atomic updates and export."""

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

from filelock import FileLock

from zunara.config import safe_name
from zunara.storage import media_path, workspace_lock, write_json

P = ParamSpec("P")
T = TypeVar("T")


def locked(function: Callable[Concatenate[Path, P], T]) -> Callable[Concatenate[Path, P], T]:
    @wraps(function)
    def wrapped(work: Path, /, *args: P.args, **kwargs: P.kwargs) -> T:
        with workspace_lock(work):
            return function(work, *args, **kwargs)

    return wrapped


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load(work: Path) -> list[dict[str, Any]]:
    path = work / "out" / "queue.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(rows, list):
        raise ValueError("queue.json must contain an array.")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Every queue entry must be an object.")
        media_path(work, row.get("file", ""))
        if row["file"] in seen:
            raise ValueError("Duplicate queue filename.")
        seen.add(row["file"])
        if not isinstance(row.get("title", ""), str):
            raise ValueError("title must be a string.")
        if not isinstance(row.get("description", ""), str):
            raise ValueError("description must be a string.")
        tags = row.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ValueError("tags must be an array of strings.")
        for field in ("sha256", "approved_sha256"):
            value = row.get(field)
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise ValueError(f"{field} must be a SHA-256 hex string or null.")
        video_id = row.get("youtube_id")
        if video_id is not None and (not isinstance(video_id, str) or not video_id.strip()):
            raise ValueError("youtube_id must be a nonempty string or null.")
        state = row.get("upload_state", "pending")
        if not isinstance(state, str) or state not in {"pending", "uploading", "uploaded"}:
            raise ValueError("Unknown upload_state.")
        if row.get("upload_state") == "uploaded" and not video_id:
            raise ValueError("Uploaded entries require youtube_id.")
    return rows


@locked
def save(work: Path, rows: list[dict[str, Any]]) -> None:
    write_json(work / "out" / "queue.json", rows)


@locked
def refresh(work: Path) -> list[dict[str, Any]]:
    if not (work / "out").is_dir():
        raise ValueError("Workspace must contain an out directory with .mp4 files.")
    rows = load(work)
    known = {r["file"]: r for r in rows}
    for path in sorted((work / "out").glob("*.mp4")):
        media_path(work, path.name)
        sha = digest(path)
        if path.name not in known:
            rows.append(
                {
                    "file": path.name,
                    "title": "",
                    "description": "",
                    "tags": [],
                    "sha256": sha,
                    "approved_sha256": None,
                    "youtube_id": None,
                    "upload_state": "pending",
                }
            )
        else:
            row = known[path.name]
            if row.get("sha256") != sha:
                row.update(sha256=sha, approved_sha256=None)
                if row.get("youtube_id") or row.get("upload_state") == "uploading":
                    raise ValueError(
                        "Previously uploaded/in-flight media changed; use a new filename."
                    )
    save(work, rows)
    return rows


def select(rows: list[dict[str, Any]], only: set[str] | None) -> list[dict[str, Any]]:
    if only is not None and not only:
        raise ValueError("Selection must not be empty; omit --only to select all.")
    if only:
        missing = only - {Path(r["file"]).stem for r in rows}
        if missing:
            raise ValueError(f"Unknown clip names: {', '.join(sorted(missing))}")
    return [r for r in rows if not only or Path(r["file"]).stem in only]


@locked
def approve(work: Path, only: set[str]) -> list[dict[str, Any]]:
    if not only:
        raise ValueError("Approval requires an explicit nonempty selection.")
    rows = refresh(work)
    chosen = select(rows, only)
    for row in chosen:
        if not row.get("title", "").strip():
            raise ValueError(f"Set a title before approval: {row['file']}")
        row["approved_sha256"] = row["sha256"]
    save(work, rows)
    return chosen


def verify_approved(work: Path, row: dict[str, Any]) -> Path:
    path = media_path(work, row["file"])
    if not row.get("approved_sha256") or digest(path) != row["approved_sha256"]:
        raise ValueError(f"Clip needs review/approval (or changed after approval): {row['file']}")
    if not row.get("title", "").strip() or len(row["title"]) > 100:
        raise ValueError("Title must contain 1–100 characters.")
    if len(row.get("description", "")) > 5000:
        raise ValueError("Description exceeds 5000 characters.")
    return path


def copy_approved(work: Path, row: dict[str, Any], destination: Path) -> Path:
    source = verify_approved(work, row)
    shutil.copyfile(source, destination)
    if digest(destination) != row["approved_sha256"]:
        destination.unlink(missing_ok=True)
        raise ValueError("Media changed while copying; review the clip again.")
    return destination


@locked
def export(work: Path, destination: Path, platform: str, only: set[str] | None) -> int:
    if platform not in {"tiktok", "instagram", "x", "youtube"}:
        raise ValueError("Unsupported export platform.")
    rows = select(load(work), only)
    # Preflight the entire batch before copying anything.
    for row in rows:
        verify_approved(work, row)
    target = destination / platform / safe_name(work.resolve().name)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Export a complete new batch. Existing batches are never overwritten or mixed.
    with FileLock(str(target.parent / f".{target.name}.lock"), timeout=10):
        if target.exists() or target.is_symlink():
            raise ValueError("Export destination already exists; choose a fresh destination.")
        with tempfile.TemporaryDirectory(prefix=".zunara-export-", dir=target.parent) as folder:
            staging = Path(folder) / "batch"
            staging.mkdir()
            for row in rows:
                copied = copy_approved(work, row, staging / row["file"])
                caption = row["title"] + "\n\n" + row.get("description", "")
                copied.with_suffix(".txt").write_text(caption.strip() + "\n", encoding="utf-8")
            write_json(
                staging / "manifest.json",
                [{"file": r["file"], "sha256": r["approved_sha256"]} for r in rows],
            )
            staging.rename(target)
    return len(rows)
