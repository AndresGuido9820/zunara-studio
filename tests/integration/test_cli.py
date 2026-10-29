"""Run the installed CLI end to end with real FFmpeg on synthetic media. No network or OAuth."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe"
    ),
]

Runner = Callable[..., subprocess.CompletedProcess[str]]


def synthesize(path: Path, *, audio: bool = True, seconds: float = 1.0) -> Path:
    """Write a tiny vertical H.264 clip, optionally with an AAC tone."""
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    args += ["-f", "lavfi", "-i", f"testsrc2=size=144x256:rate=15:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        args += ["-c:a", "aac", "-b:a", "32k"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest"]
    subprocess.run([*args, str(path)], check=True, capture_output=True, timeout=60)
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media")
    return {
        "av": synthesize(root / "av.mp4"),
        "silent": synthesize(root / "silent.mp4", audio=False),
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def zunara(home: Path) -> Runner:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "ZUNARA_CONFIG_DIR": str(home / "config"),
        "ZUNARA_DATA_DIR": str(home / "data"),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "zunara", *map(str, args)],
            capture_output=True,
            text=True,
            env=env,
            cwd=home,
            timeout=120,
        )

    return run


def ok(result: subprocess.CompletedProcess[str]) -> Any:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def failed(result: subprocess.CompletedProcess[str], message: str) -> str:
    assert result.returncode == 2, result.stdout
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    return result.stderr


@pytest.fixture
def work(tmp_path: Path, clips: dict[str, Path]) -> Path:
    root = tmp_path / "session-01"
    (root / "out").mkdir(parents=True)
    shutil.copy(clips["av"], root / "out" / "goal.mp4")
    shutil.copy(clips["av"], root / "out" / "save.mp4")
    return root


def set_titles(work: Path, **titles: str) -> None:
    path = work / "out" / "queue.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        row["title"] = titles.get(Path(row["file"]).stem, row["title"])
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


# Basics


def test_version(zunara: Runner) -> None:
    from zunara import __version__

    result = zunara("--version")

    assert result.returncode == 0
    assert result.stdout.strip() == __version__


def test_console_script_is_installed() -> None:
    script = Path(sys.executable).with_name("zunara")
    if not script.exists():
        pytest.skip("package not installed with console scripts")

    result = subprocess.run([str(script), "--help"], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0
    assert "publish" in result.stdout


def test_doctor_reports_tools_without_creating_files(zunara: Runner, home: Path) -> None:
    report = ok(zunara("doctor"))

    assert report["tools"]["ffmpeg"] is True
    assert report["tools"]["ffprobe"] is True
    assert report["config_dir"] == str(home / "config")
    assert report["data_dir"] == str(home / "data")
    assert report["youtube_client_configured"] is False
    assert list(home.iterdir()) == []


def test_missing_subcommand_is_a_usage_error(zunara: Runner) -> None:
    result = zunara()

    assert result.returncode == 2
    assert "usage" in result.stderr


# inspect


def test_inspect_reads_real_media(zunara: Runner, clips: dict[str, Path]) -> None:
    info = ok(zunara("inspect", clips["av"]))

    assert (info["width"], info["height"]) == (144, 256)
    assert info["codec"] == "h264"
    assert info["has_audio"] is True
    assert info["duration_s"] == pytest.approx(1.0, abs=0.2)


def test_inspect_detects_missing_audio(zunara: Runner, clips: dict[str, Path]) -> None:
    assert ok(zunara("inspect", clips["silent"]))["has_audio"] is False


def test_inspect_handles_dash_prefixed_filename(
    zunara: Runner, clips: dict[str, Path], home: Path
) -> None:
    shutil.copy(clips["av"], home / "-i.mp4")

    assert ok(zunara("inspect", "./-i.mp4"))["width"] == 144


@pytest.mark.parametrize("content", [b"", b"not a video at all", b"\x00" * 4096])
def test_inspect_rejects_non_video_without_leaking_details(
    zunara: Runner, tmp_path: Path, content: bytes
) -> None:
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(content)

    result = zunara("inspect", fake)

    assert result.returncode == 2
    assert result.stderr.startswith("zunara: ")
    assert "Traceback" not in result.stderr
    assert str(tmp_path) not in result.stderr


def test_inspect_audio_only_file_has_no_video(zunara: Runner, tmp_path: Path) -> None:
    tone = tmp_path / "tone.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1"]
        + ["-c:a", "aac", str(tone)],
        check=True,
        capture_output=True,
        timeout=60,
    )

    failed(zunara("inspect", tone), "no video stream")


# Review workflow


def test_queue_status_approve_export(zunara: Runner, work: Path, tmp_path: Path) -> None:
    rows = ok(zunara("queue", work))
    assert [r["file"] for r in rows] == ["goal.mp4", "save.mp4"]
    assert {r["upload_state"] for r in rows} == {"pending"}
    assert ok(zunara("status", work)) == rows

    set_titles(work, goal="Golazo ⚽")
    approved = ok(zunara("approve", work, "--only", "goal"))
    assert [r["file"] for r in approved] == ["goal.mp4"]

    destination = tmp_path / "ready"
    assert ok(
        zunara(
            "export", work, "--platform", "tiktok", "--destination", destination, "--only", "goal"
        )
    ) == {"exported": 1}

    target = destination / "tiktok" / "session-01"
    exported = target / "goal.mp4"
    assert exported.read_bytes() == (work / "out" / "goal.mp4").read_bytes()
    assert (target / "goal.txt").read_text(encoding="utf-8").startswith("Golazo ⚽")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == [
        {"file": "goal.mp4", "sha256": hashlib.sha256(exported.read_bytes()).hexdigest()}
    ]
    assert not (target / "save.mp4").exists()


def test_export_defaults_to_data_dir(zunara: Runner, work: Path, home: Path) -> None:
    ok(zunara("queue", work))
    set_titles(work, goal="Goal")
    ok(zunara("approve", work, "--only", "goal"))

    ok(zunara("export", work, "--platform", "x", "--only", "goal"))

    assert (home / "data" / "ready" / "x" / "session-01" / "goal.mp4").is_file()


def test_approve_requires_explicit_selection(zunara: Runner, work: Path) -> None:
    ok(zunara("queue", work))
    set_titles(work, goal="Goal", save="Save")

    failed(zunara("approve", work), "--only")
    failed(zunara("approve", work, "--only", " , "), "--only")

    rows = ok(zunara("status", work))
    assert all(r["approved_sha256"] is None for r in rows)


def test_approve_unknown_clip(zunara: Runner, work: Path) -> None:
    ok(zunara("queue", work))
    set_titles(work, goal="Goal")

    failed(zunara("approve", work, "--only", "goal,typo"), "Unknown clip names: typo")
    assert all(r["approved_sha256"] is None for r in ok(zunara("status", work)))


def test_approve_requires_title(zunara: Runner, work: Path) -> None:
    ok(zunara("queue", work))

    failed(zunara("approve", work, "--only", "goal"), "Set a title")


def test_re_rendered_clip_needs_new_approval(
    zunara: Runner, work: Path, clips: dict[str, Path], tmp_path: Path
) -> None:
    ok(zunara("queue", work))
    set_titles(work, goal="Goal")
    ok(zunara("approve", work, "--only", "goal"))
    shutil.copy(clips["silent"], work / "out" / "goal.mp4")

    failed(
        zunara(
            "export",
            work,
            "--platform",
            "tiktok",
            "--destination",
            tmp_path / "ready",
            "--only",
            "goal",
        ),
        "approval",
    )
    assert not (tmp_path / "ready").exists()

    rows = {r["file"]: r for r in ok(zunara("queue", work))}
    assert rows["goal.mp4"]["approved_sha256"] is None


def test_export_rejects_unknown_platform(zunara: Runner, work: Path) -> None:
    result = zunara("export", work, "--platform", "facebook")

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_queue_refuses_symlink_escape(zunara: Runner, work: Path, clips: dict[str, Path]) -> None:
    (work / "out" / "leak.mp4").symlink_to(clips["av"])

    failed(zunara("queue", work), "inside the workspace")
    assert not (work / "out" / "queue.json").exists()


@pytest.mark.parametrize(
    "content",
    [
        "{",
        '{"file": "goal.mp4"}',
        '[{"file": "../goal.mp4"}]',
        '[{"file": "goal.mp4", "tags": "x"}]',
    ],
)
def test_status_rejects_malformed_queue(zunara: Runner, work: Path, content: str) -> None:
    (work / "out" / "queue.json").write_text(content, encoding="utf-8")

    result = zunara("status", work)

    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_queue_requires_out_directory(zunara: Runner, tmp_path: Path) -> None:
    failed(zunara("queue", tmp_path), "out directory")


# Publishing stays local without authorization


def test_publish_without_token_stops_before_network(zunara: Runner, work: Path, home: Path) -> None:
    ok(zunara("queue", work))
    set_titles(work, goal="Goal")
    ok(zunara("approve", work, "--only", "goal"))
    before = (work / "out" / "queue.json").read_bytes()

    result = zunara(
        "publish",
        work,
        "--channel",
        "main",
        "--channel-id",
        "UCabcdefghijklmnopqrstuv",
        "--execute",
    )

    assert result.returncode == 2
    assert "Authorize this channel first" in result.stderr or "YouTube extra" in result.stderr
    assert (work / "out" / "queue.json").read_bytes() == before
    assert not (home / "config" / "tokens").exists()


@pytest.mark.parametrize("channel", ["../main", "a/b", ".hidden"])
def test_publish_rejects_unsafe_channel_alias(
    zunara: Runner, work: Path, home: Path, channel: str
) -> None:
    result = zunara("publish", work, "--channel", channel, "--channel-id", "UCx")

    assert result.returncode == 2
    assert not (home / "config").exists()
