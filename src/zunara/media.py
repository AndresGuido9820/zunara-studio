"""Local media inspection, with a bounded subprocess and no shell interpretation."""

import json
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Any


def executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ValueError(f"{name} is required; install FFmpeg and ensure it is on PATH.")
    return path


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(  # nosec B603
        [
            executable("ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path.resolve()),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = json.loads(result.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("File has no video stream.")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration_s": float(data["format"]["duration"]),
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "has_audio": audio is not None,
        "audio_codec": audio["codec_name"] if audio else None,
        "audio_sample_rate": _rate(audio),
    }


def _rate(stream: dict[str, Any] | None) -> int | None:
    """ffprobe reports sample_rate as a decimal string; anything else is unknown."""
    value = stream.get("sample_rate") if stream else None
    return int(value) if isinstance(value, str) and value.isdigit() else None
