import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from zunara import media

FFPROBE = "/opt/ffmpeg/bin/ffprobe"


def ffprobe_output(streams: list[dict[str, Any]], duration: str = "12.5") -> str:
    return json.dumps({"streams": streams, "format": {"duration": duration}})


VIDEO = {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920}
AUDIO = {"codec_type": "audio", "codec_name": "aac"}


class FakeFfprobe:
    def __init__(self) -> None:
        self.stdout = ffprobe_output([VIDEO, AUDIO])
        self.calls: list[dict[str, Any]] = []

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr="")


@pytest.fixture
def fake_ffprobe(monkeypatch: pytest.MonkeyPatch) -> FakeFfprobe:
    fake = FakeFfprobe()
    monkeypatch.setattr(media.shutil, "which", lambda name: FFPROBE if name == "ffprobe" else None)
    monkeypatch.setattr(media.subprocess, "run", fake.run)
    return fake


def test_probe_invokes_ffprobe_safely(
    fake_ffprobe: FakeFfprobe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("-i.mp4").write_bytes(b"x")

    media.probe(Path("-i.mp4"))

    [call] = fake_ffprobe.calls
    assert call["args"][0] == FFPROBE
    assert call["args"][1:-1] == ["-v", "error", "-show_format", "-show_streams", "-of", "json"]
    assert call["args"][-1] == str(tmp_path / "-i.mp4")
    assert call.get("shell", False) is False
    assert call["check"] is True
    assert call["timeout"] == 60
    assert call["capture_output"] is True


def test_probe_parses_video_and_audio(fake_ffprobe: FakeFfprobe, tmp_path: Path) -> None:
    assert media.probe(tmp_path / "a.mp4") == {
        "duration_s": 12.5,
        "width": 1080,
        "height": 1920,
        "codec": "h264",
        "has_audio": True,
    }


def test_probe_reports_missing_audio(fake_ffprobe: FakeFfprobe, tmp_path: Path) -> None:
    fake_ffprobe.stdout = ffprobe_output([VIDEO])

    assert media.probe(tmp_path / "a.mp4")["has_audio"] is False


def test_probe_uses_first_video_stream(fake_ffprobe: FakeFfprobe, tmp_path: Path) -> None:
    cover = {"codec_type": "video", "codec_name": "mjpeg", "width": 320, "height": 320}
    fake_ffprobe.stdout = ffprobe_output([AUDIO, VIDEO, cover])

    assert media.probe(tmp_path / "a.mp4")["codec"] == "h264"


@pytest.mark.parametrize("streams", [[], [AUDIO], [{"codec_type": "subtitle"}]])
def test_probe_rejects_files_without_video(
    fake_ffprobe: FakeFfprobe, tmp_path: Path, streams: list[dict[str, Any]]
) -> None:
    fake_ffprobe.stdout = ffprobe_output(streams)

    with pytest.raises(ValueError, match="no video stream"):
        media.probe(tmp_path / "a.mp4")


@pytest.mark.parametrize("stdout", ["not json", ffprobe_output([VIDEO], duration="N/A")])
def test_probe_rejects_unparseable_output(
    fake_ffprobe: FakeFfprobe, tmp_path: Path, stdout: str
) -> None:
    fake_ffprobe.stdout = stdout

    with pytest.raises(ValueError):
        media.probe(tmp_path / "a.mp4")


def test_probe_propagates_ffprobe_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(args: list[str], **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, args, stderr="Invalid data")

    monkeypatch.setattr(media.shutil, "which", lambda name: FFPROBE)
    monkeypatch.setattr(media.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        media.probe(tmp_path / "broken.mp4")


def test_missing_ffprobe_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def never_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("subprocess must not run without ffprobe")

    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    monkeypatch.setattr(media.subprocess, "run", never_run)

    with pytest.raises(ValueError, match="ffprobe is required"):
        media.probe(tmp_path / "a.mp4")
