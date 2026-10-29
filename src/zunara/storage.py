"""Atomic JSON persistence and bounded paths for user-managed workspaces."""

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout


@contextmanager
def workspace_lock(work: Path) -> Iterator[None]:
    out = work / "out"
    if out.is_symlink() or not out.is_dir():
        raise ValueError("Workspace must contain a real out directory, not a symlink.")
    # A singleton makes nested service calls reentrant within the same thread.
    lock = FileLock(str(out.resolve() / ".zunara.lock"), timeout=10, is_singleton=True)
    try:
        with lock:
            yield
    except Timeout as error:
        raise ValueError("Workspace is busy; wait for the other command to finish.") from error


def write_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    if private:
        if path.parent.is_symlink():
            raise ValueError("Token directory must not be a symlink.")
        path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def media_path(work: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".mp4"):
        raise ValueError("Queue file must be a plain .mp4 filename, without directories.")
    if (work / "out").is_symlink():
        raise ValueError("Media must stay inside the workspace; out must not be a symlink.")
    root = (work / "out").resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError("Media must stay inside the workspace out directory (including symlinks).")
    if not candidate.is_file():
        raise ValueError(f"Missing media: {name}")
    return candidate
