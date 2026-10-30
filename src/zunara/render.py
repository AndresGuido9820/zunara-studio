"""Render a vertical 9:16 short from a VOD segment in a single ffmpeg pass.

Layout (built for IRL wide-angle streams, where a centre crop cuts people out):
blurred full frame as background, the 4:3 centre of the frame in the middle,
a hook title on top and word-by-word captions below. Captions come from
Parakeet TDT v3 (int8, CPU) run only on the clip audio. Encoding uses the
Intel GPU through VAAPI (h264_vaapi) and falls back to libx264 without it.

With a local source file (e.g. a downloaded MP4) the clip is cut from that file
instead of the workspace VOD: only the selected audio is transcribed, the source
is never modified and existing outputs are never replaced.

Usage:
    python3 render.py <work_dir> <start_s> <end_s> "<HOOK TEXT>" <out_name> [4:3|full]
"""

from __future__ import annotations

import contextlib
import math
import os
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path

import soundfile as sf

from zunara.config import safe_name
from zunara.media import executable, probe

FPS = 30
# Middle band: "4:3" crops the centre (IRL, single camera); "full" keeps the
# whole 16:9 frame (reactions with a webcam overlay in a corner).
LAYOUTS = {"4:3": ("crop=ih*4/3:ih,scale=1080:810", 555), "full": ("scale=1080:608", 656)}
WORDS_PER_LINE = 3
_ASR = None


def asr_model():
    global _ASR
    if _ASR is None:
        import onnx_asr

        _ASR = onnx_asr.load_model(
            "nemo-parakeet-tdt-0.6b-v3", quantization="int8"
        ).with_timestamps()
    return _ASR


def transcribe_words(wav_path: Path, start: float, end: float, piece_s: float = 20.0) -> list[dict]:
    """Word-level timestamps (relative to start) for one slice of the VOD audio."""
    with sf.SoundFile(str(wav_path)) as f:
        sr = f.samplerate
        f.seek(int(start * sr))
        audio = f.read(int((end - start) * sr), dtype="float32", always_2d=True).mean(axis=1)
    words = []
    for i in range(0, len(audio), int(piece_s * sr)):
        result = asr_model().recognize(audio[i : i + int(piece_s * sr)], sample_rate=sr)
        base = i / sr
        for token, ts in zip(result.tokens, result.timestamps, strict=True):
            if token.startswith(" ") or not words:
                words.append({"text": token.strip(), "start": base + ts, "end": base + ts + 0.3})
            else:
                words[-1]["text"] += token
                words[-1]["end"] = base + ts + 0.3
    for cur, nxt in zip(words, words[1:], strict=False):
        cur["end"] = min(cur["end"], nxt["start"])
    return [w for w in words if w["text"]]


def escape_ass(text: str) -> str:
    return (
        text.replace("\\", "＼")
        .replace("{", "｛")
        .replace("}", "｝")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(words: list[dict], hook: str, duration: float) -> str:
    """ASS subtitles: hook title on top, 3-word caption lines with the spoken word in yellow."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Anton,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,1,0,1,7,3,8,70,70,190,1
Style: Cap,Anton,104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,0,0,0,0,100,100,1,0,1,8,4,2,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [
        f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Hook,,0,0,0,,{escape_ass(hook.upper())}"
    ]
    shown = [escape_ass(w["text"].strip(".,;:¿?¡!…").upper()) for w in words]
    for i in range(0, len(words), WORDS_PER_LINE):
        line = words[i : i + WORDS_PER_LINE]
        next_line_start = (
            words[i + WORDS_PER_LINE]["start"] if i + WORDS_PER_LINE < len(words) else duration
        )
        for j, w in enumerate(line):
            end = (
                line[j + 1]["start"] if j + 1 < len(line) else min(w["end"] + 0.25, next_line_start)
            )
            parts = [
                ("{\\c&H00E5FF&}" + shown[i + k] + "{\\c&HFFFFFF&}") if k == j else shown[i + k]
                for k in range(len(line))
            ]
            events.append(
                f"Dialogue: 0,{_ass_time(w['start'])},{_ass_time(end)},Cap,,0,0,0,,{' '.join(parts)}"
            )
    return header + "\n".join(events) + "\n"


VAAPI_DEVICE = "/dev/dri/renderD128"


def has_vaapi() -> bool:
    probe = subprocess.run(
        [
            executable("ffmpeg"),
            "-hide_banner",
            "-vaapi_device",
            VAAPI_DEVICE,
            "-f",
            "lavfi",
            "-i",
            "color=black:s=256x256:d=0.1",
            "-vf",
            "format=nv12,hwupload",
            "-c:v",
            "h264_vaapi",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=30,
    )  # nosec B603
    return probe.returncode == 0


def check_local_source(source: Path, start: float, end: float) -> Path:
    """Resolve a local source and check it has video, audio and covers [start, end)."""
    source = source.resolve()
    if not source.is_file():
        raise ValueError("Source must be an existing local video file.")
    try:
        info = probe(source)
    except (subprocess.SubprocessError, KeyError, TypeError, ValueError):
        # Covers no video stream and unparsable files; ffprobe output may echo the path.
        raise ValueError("Source is not a readable video file.") from None
    if not info["has_audio"]:
        raise ValueError("Source has no audio stream; captions need the original audio.")
    if end > info["duration_s"]:
        raise ValueError(
            f"Render interval ends after the source ({info['duration_s']:.2f}s); choose end <= it."
        )
    return source


def extract_audio(source: Path, start: float, duration: float, wav_path: Path) -> None:
    """Write only the selected source audio as 16 kHz mono WAV for ASR."""
    subprocess.run(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            f"file:{source}",
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            # Keep the segment on the video timeline: pad a late audio start and a short end.
            "-af",
            "aresample=async=1:first_pts=0,apad",
            "-t",
            str(duration),
            str(wav_path),
        ],
        check=True,
        capture_output=True,
        timeout=3600,
    )  # nosec B603


def render(
    work: Path,
    start: float,
    end: float,
    hook: str,
    out_name: str,
    layout: str = "4:3",
    source: Path | None = None,
) -> Path:
    safe_name(out_name)
    if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
        raise ValueError("Render interval must be finite and satisfy 0 <= start < end.")
    if layout not in LAYOUTS:
        raise ValueError("Unknown layout.")
    work = work.resolve()
    fg_filter, fg_y = LAYOUTS[layout]
    duration = end - start
    out_dir = work / "out"
    ass_path = out_dir / f"{out_name}.ass"
    out_path = out_dir / f"{out_name}.mp4"
    if source is not None:
        # Validate everything before loading the model or writing any file.
        source = check_local_source(source, start, end)
        if ass_path.exists() or out_path.exists():
            raise ValueError(f"Output {out_name} already exists; choose another --name.")
    t0 = time.time()
    if source is None:
        words = transcribe_words(work / "audio16k.wav", start, end)
    else:
        with tempfile.TemporaryDirectory(prefix="zunara-") as tmp:
            wav_path = Path(tmp) / "segment.wav"
            extract_audio(source, start, duration, wav_path)
            words = transcribe_words(wav_path, 0, duration)
    t_asr = time.time() - t0
    if source is None:
        url = (work / "base_url.txt").read_text().strip() + "/1080p60/playlist.m3u8"
        inputs = ["-user_agent", "Mozilla/5.0", "-ss", str(start), "-t", str(duration)]
        inputs += ["-i", url]
        audio = "0:a"
    else:
        inputs = ["-ss", str(start), "-t", str(duration), "-i", f"file:{source}"]
        audio = "0:a:0"
    graph = (
        f"[0:v]fps={FPS},split[a][b];"
        "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        "boxblur=8:2,scale=1080:1920,eq=brightness=-0.18[bg];"
        f"[b]{fg_filter}[fg];"
        f"[bg][fg]overlay=0:{fg_y},ass={ass_path.name},format=nv12"
    )
    if has_vaapi():
        device = ["-vaapi_device", VAAPI_DEVICE]
        graph += ",hwupload[v]"
        encoder = ["-c:v", "h264_vaapi", "-qp", "22"]
    else:
        device = []
        graph += "[v]"
        encoder = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Local-source renders never replace files: the .ass is created exclusively and
        # the video is staged privately, then linked into place only if the name is free.
        ass_file = ass_path.open("w" if source is None else "x", encoding="utf-8")
    except FileExistsError:
        raise ValueError(f"Output {out_name} already exists; choose another --name.") from None
    t1 = time.time()
    with contextlib.ExitStack() as stack:
        target = out_path
        try:
            with ass_file:
                ass_file.write(build_ass(words, hook, duration))
            if source is not None:
                staging = stack.enter_context(
                    tempfile.TemporaryDirectory(dir=out_dir, prefix=f".{out_name}-")
                )
                target = Path(staging) / out_path.name
            subprocess.run(
                [
                    executable("ffmpeg"),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    *device,
                    *inputs,
                    "-filter_complex",
                    graph,
                    "-map",
                    "[v]",
                    "-map",
                    audio,
                    *encoder,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-af",
                    "loudnorm=I=-14:TP=-1.5:LRA=11",
                    "-movflags",
                    "+faststart",
                    str(target),
                ],
                check=True,
                cwd=out_dir,
                timeout=3600,
            )  # nosec B603
            if source is not None:
                try:
                    os.link(target, out_path)
                except FileExistsError:
                    raise ValueError(
                        f"Output {out_name} already exists; choose another --name."
                    ) from None
                except OSError:
                    raise ValueError(
                        "Could not place the clip: the workspace filesystem must support "
                        "hard links (e.g. not FAT/exFAT)."
                    ) from None
        except BaseException:
            if source is not None:
                # Only the .ass was created by this call; any out_path belongs to someone else.
                ass_path.unlink(missing_ok=True)
            raise
    print(
        f"{out_name}: {duration:.0f}s clip, asr {t_asr:.1f}s, render {time.time() - t1:.1f}s "
        f"({encoder[1]}), {len(words)} words -> {out_path}",
        file=sys.stderr,
    )
    return out_path


if __name__ == "__main__":
    work_dir, start_s, end_s, hook_text, name = sys.argv[1:6]
    render(Path(work_dir), float(start_s), float(end_s), hook_text, name, *sys.argv[6:7])
