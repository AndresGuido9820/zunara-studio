"""Render from a local source file with real FFmpeg on synthetic media and a fake ASR model."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from zunara import render as render_module

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe"
    ),
]


def synthesize(
    path: Path, *, audio: bool = True, seconds: float = 6.0, audio_delay: float = 0
) -> Path:
    """Write a small 16:9 H.264 clip, optionally with an AAC tone starting after audio_delay."""
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    args += ["-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}"]
    if audio:
        args += ["-itsoffset", str(audio_delay), "-f", "lavfi"]
        args += ["-i", f"sine=frequency=440:duration={seconds - audio_delay}"]
        args += ["-c:a", "aac", "-b:a", "64k"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    subprocess.run([*args, str(path)], check=True, capture_output=True, timeout=60)
    return path


def ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeAsr:
    """Stands in for Parakeet: records the audio it gets and returns two words."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, float]] = []
        self.audio: list[np.ndarray] = []

    def recognize(self, audio: np.ndarray, sample_rate: int) -> SimpleNamespace:
        self.calls.append((len(audio), sample_rate, float(np.abs(audio).max())))
        self.audio.append(audio)
        return SimpleNamespace(tokens=[" hola", " mun", "do"], timestamps=[0.1, 0.6, 0.8])


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("sources")
    return {
        "av": synthesize(root / "stream.mp4"),
        "silent": synthesize(root / "silent.mp4", audio=False),
        "late": synthesize(root / "late.mp4", audio_delay=1.5),
    }


@pytest.fixture
def source(tmp_path: Path, media: dict[str, Path]) -> Path:
    path = tmp_path / "downloads" / "-stream: part.mp4"
    path.parent.mkdir()
    shutil.copy(media["av"], path)
    return path


@pytest.fixture
def asr(monkeypatch: pytest.MonkeyPatch) -> FakeAsr:
    fake = FakeAsr()
    monkeypatch.setattr(render_module, "_ASR", fake)
    return fake


@pytest.mark.parametrize("hardware", [False, True], ids=["libx264", "vaapi"])
def test_local_source_renders_selected_segment(
    tmp_path: Path, source: Path, asr: FakeAsr, monkeypatch: pytest.MonkeyPatch, hardware: bool
) -> None:
    if hardware and not render_module.has_vaapi():
        pytest.skip("no VAAPI device")
    monkeypatch.setattr(render_module, "has_vaapi", lambda: hardware)
    work = tmp_path / "work"
    before = digest(source)

    out = render_module.render(work, 1.5, 3.5, "Hook {x}", "clip", source=source)

    assert out == work.resolve() / "out" / "clip.mp4"
    assert digest(source) == before
    # Only the selected 2 s of audio, resampled for ASR, reached the model.
    assert len(asr.calls) == 1
    samples, rate, peak = asr.calls[0]
    assert rate == 16000
    assert samples == pytest.approx(2 * 16000, abs=800)
    assert peak > 0.1
    info = ffprobe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1080, 1920)
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert float(info["format"]["duration"]) == pytest.approx(2.0, abs=0.15)
    ass = (work / "out" / "clip.ass").read_text(encoding="utf-8")
    assert "HOLA" in ass and "MUNDO" in ass
    assert "HOOK ｛X｝" in ass
    assert sorted(p.name for p in (work / "out").iterdir()) == ["clip.ass", "clip.mp4"]


def test_existing_outputs_are_not_replaced(tmp_path: Path, source: Path, asr: FakeAsr) -> None:
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    existing = work / "out" / "clip.mp4"
    existing.write_bytes(b"reviewed clip")

    with pytest.raises(ValueError, match="already exists"):
        render_module.render(work, 0, 2, "Hook", "clip", source=source)

    assert existing.read_bytes() == b"reviewed clip"
    assert not (work / "out" / "clip.ass").exists()
    assert asr.calls == []


def test_late_audio_stays_aligned_with_video(
    tmp_path: Path, media: dict[str, Path], asr: FakeAsr
) -> None:
    render_module.render(tmp_path / "work", 1, 3, "Hook", "clip", source=media["late"])

    # Audio starts 1.5 s into the source: the ASR segment keeps 0.5 s of leading silence.
    (audio,) = asr.audio
    assert len(audio) == pytest.approx(2 * 16000, abs=160)
    assert np.abs(audio[: int(0.4 * 16000)]).max() < 0.01
    assert np.abs(audio[int(0.7 * 16000) :]).max() > 0.1


@pytest.mark.parametrize("local", [False, True], ids=["vod", "local"])
def test_user_agent_is_only_sent_to_the_remote_vod(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "base_url.txt").write_text("https://vod.example/abc")
    real_run = subprocess.run
    renders: list[list[str]] = []

    def recording_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if "-filter_complex" not in cmd:
            return real_run(cmd, *args, **kwargs)
        renders.append(cmd)
        Path(cmd[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(render_module, "transcribe_words", lambda *_: [])
    monkeypatch.setattr(render_module, "has_vaapi", lambda: False)
    monkeypatch.setattr(render_module.subprocess, "run", recording_run)

    render_module.render(work, 0, 2, "Hook", "clip", source=source if local else None)

    (cmd,) = renders
    source_arg = cmd[cmd.index("-i") + 1]
    if local:
        assert "-user_agent" not in cmd
        assert source_arg == f"file:{source.resolve()}"
    else:
        assert cmd[cmd.index("-user_agent") : cmd.index("-i")][:2] == ["-user_agent", "Mozilla/5.0"]
        assert source_arg == "https://vod.example/abc/1080p60/playlist.m3u8"


@pytest.mark.parametrize("encode_fails", [False, True], ids=["encoded", "encode-failed"])
def test_output_created_during_render_is_never_replaced_or_deleted(
    tmp_path: Path,
    source: Path,
    asr: FakeAsr,
    monkeypatch: pytest.MonkeyPatch,
    encode_fails: bool,
) -> None:
    work = tmp_path / "work"
    final = work / "out" / "clip.mp4"
    real_run = subprocess.run

    def racing_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if "-filter_complex" in cmd:
            # Another process claims the name after the precheck, while this one encodes.
            final.write_bytes(b"concurrent clip")
            if encode_fails:
                raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(render_module.subprocess, "run", racing_run)
    before = digest(source)

    with pytest.raises(
        subprocess.CalledProcessError if encode_fails else ValueError,  # type: ignore[arg-type]
    ):
        render_module.render(work, 0, 2, "Hook", "clip", source=source)

    assert final.read_bytes() == b"concurrent clip"
    # The staged video and this call's .ass are gone; nothing else is touched.
    assert [p.name for p in (work / "out").iterdir()] == ["clip.mp4"]
    assert digest(source) == before


def test_ass_created_during_transcription_is_kept(
    tmp_path: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    ass = work / "out" / "clip.ass"

    def racing_transcribe(*_: Any) -> list[dict[str, Any]]:
        ass.parent.mkdir(parents=True)
        ass.write_text("other render", encoding="utf-8")
        return []

    monkeypatch.setattr(render_module, "transcribe_words", racing_transcribe)

    with pytest.raises(ValueError, match="already exists"):
        render_module.render(work, 0, 2, "Hook", "clip", source=source)

    assert ass.read_text(encoding="utf-8") == "other render"
    assert [p.name for p in (work / "out").iterdir()] == ["clip.ass"]


@pytest.mark.parametrize(("start", "end"), [(0, 6.5), (5, 60), (-1, 2), (3, 3)])
def test_interval_outside_source_fails_before_model_or_output(
    tmp_path: Path, source: Path, asr: FakeAsr, start: float, end: float
) -> None:
    work = tmp_path / "work"

    with pytest.raises(ValueError, match="interval"):
        render_module.render(work, start, end, "Hook", "clip", source=source)

    assert asr.calls == []
    assert not work.exists()


def test_source_without_audio_or_file_is_rejected(
    tmp_path: Path, media: dict[str, Path], asr: FakeAsr
) -> None:
    work = tmp_path / "work"

    with pytest.raises(ValueError, match="no audio"):
        render_module.render(work, 0, 2, "Hook", "clip", source=media["silent"])
    with pytest.raises(ValueError, match="existing local video"):
        render_module.render(work, 0, 2, "Hook", "clip", source=tmp_path)
    with pytest.raises(ValueError, match="existing local video"):
        render_module.render(work, 0, 2, "Hook", "clip", source=tmp_path / "missing.mp4")
    notes = tmp_path / "notes.txt"
    notes.write_text("not a video")
    with pytest.raises(ValueError, match="not a readable video"):
        render_module.render(work, 0, 2, "Hook", "clip", source=notes)

    assert asr.calls == []
    assert not work.exists()


# The installed CLI has no ASR extra in CI, so these checks must fail before any model import.


def cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "zunara", *map(str, args)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_cli_documents_source_flag() -> None:
    result = cli("render", "--help")

    assert result.returncode == 0
    assert "--source PATH" in result.stdout


@pytest.mark.parametrize(
    ("end", "fixture", "message"),
    [("60", "av", "ends after the source"), ("2", "silent", "no audio stream")],
)
def test_cli_rejects_invalid_source_before_output(
    tmp_path: Path, media: dict[str, Path], end: str, fixture: str, message: str
) -> None:
    work = tmp_path / "work"

    result = cli(
        "render", work, "0", end, "--hook", "H", "--name", "clip", "--source", media[fixture]
    )

    assert result.returncode == 2, result.stdout
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert not work.exists()
