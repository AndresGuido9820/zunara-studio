"""Cheap per-second signals for a VOD: audio loudness, chat rate and transcript.

Nothing here needs a model: loudness comes from the 16 kHz mono track read in
blocks, chat rate from sampled Kick chat replay, and the transcript from the
Kick auto-generated WebVTT subtitles (rolling cues collapsed into phrases).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import soundfile as sf

CUE_RE = re.compile(r"(\d+):(\d+):(\d+)\.(\d+) --> (\d+):(\d+):(\d+)\.(\d+)")


def loudness_per_second(wav_path: str | Path, block_s: int = 600) -> np.ndarray:
    """RMS loudness in dB for every second of the track."""
    out = []
    with sf.SoundFile(str(wav_path)) as f:
        sr = f.samplerate
        while True:
            block = f.read(sr * block_s, dtype="float32", always_2d=True).mean(axis=1)
            if not len(block):
                break
            n = len(block) // sr
            sec = block[: n * sr].reshape(n, sr)
            out.append(20 * np.log10(np.sqrt((sec**2).mean(axis=1)) + 1e-6))
    return np.concatenate(out) if out else np.array([], dtype=float)


def chat_per_second(samples_path: str | Path, duration_s: int) -> np.ndarray:
    """Interpolate sampled chat messages-per-minute onto a per-second grid."""
    rows = sorted(
        (r for r in json.loads(Path(samples_path).read_text()) if r.get("per_min") is not None),
        key=lambda r: r["offset"],
    )
    if not rows:
        return np.zeros(duration_s)
    xs = np.array([r["offset"] for r in rows], dtype=float)
    ys = np.array([min(r["per_min"], 2000.0) for r in rows])
    return np.interp(np.arange(duration_s), xs, ys)


def parse_rolling_vtt(vtt_path: str | Path) -> list[dict]:
    """Collapse rolling auto-captions into non-overlapping phrases with times."""
    text = Path(vtt_path).read_text(encoding="utf-8", errors="ignore")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.strip().splitlines()]
        for i, ln in enumerate(lines):
            m = CUE_RE.search(ln)
            if m:
                g = list(map(int, m.groups()))
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                body = " ".join(lines[i + 1 :]).strip()
                if body:
                    cues.append((start, end, body))
                break
    cues.sort()
    # A caption line grows cue by cue (with small corrections) until it resets;
    # keep only the final, longest version of each line.
    phrases = []
    for start, end, body in cues:
        last = phrases[-1] if phrases else None
        if last and body[:20].lower() == last["text"][:20].lower():
            last["text"], last["end"] = body, end
        else:
            phrases.append({"start": start, "end": end, "text": body})
    return phrases


def transcript_window(phrases: list[dict], start: float, end: float) -> str:
    """Plain text spoken between start and end seconds."""
    return " ".join(p["text"] for p in phrases if p["end"] > start and p["start"] < end)


def zscore(x: np.ndarray, win: int = 600) -> np.ndarray:
    """Z-score against a rolling 10-minute baseline, so quiet and loud parts compare fairly."""
    if not len(x):
        return x.copy()
    win = max(1, min(win, len(x)))
    kernel = np.ones(win) / win
    mean = np.convolve(x, kernel, mode="same")
    var = np.convolve((x - mean) ** 2, kernel, mode="same")
    return (x - mean) / (np.sqrt(var) + 1e-6)


def smooth(x: np.ndarray, win: int) -> np.ndarray:
    if not len(x):
        return x.copy()
    win = max(1, min(win, len(x)))
    return np.convolve(x, np.ones(win) / win, mode="same")
