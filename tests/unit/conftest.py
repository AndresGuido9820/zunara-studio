import json
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """A workspace named ``session-01`` with an empty ``out`` directory."""
    root = tmp_path / "session-01"
    (root / "out").mkdir(parents=True)
    return root


@pytest.fixture
def add_clip(work: Path) -> Callable[..., Path]:
    def add(name: str, content: bytes = b"frames") -> Path:
        path = work / "out" / name
        path.write_bytes(content)
        return path

    return add


@pytest.fixture
def write_queue(work: Path) -> Callable[[object], Path]:
    def write(rows: object) -> Path:
        path = work / "out" / "queue.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return path

    return write
