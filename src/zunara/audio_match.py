"""Locate short audio clips inside a long VOD audio track.

Both signals are reduced to log-mel frames (10 frames per second). Each query
is split into fixed-size chunks and every chunk is matched against the whole
VOD with normalized cross-correlation, so compilations and re-cut edits from
third-party channels still resolve to their original offsets. Runs on CPU in
seconds for multi-hour VODs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import librosa
import numpy as np
from scipy.signal import fftconvolve

SAMPLE_RATE = 16000
HOP = 1600  # 100 ms per frame
N_MELS = 40
CHUNK_S = 8.0
MIN_CONFIDENCE = 4.0  # peak z-score over the correlation curve


def mel_frames(path: str | Path, block_s: float = 600.0) -> np.ndarray:
    """Return z-normalized log-mel features (frames x bands), read in blocks."""
    feats = []
    for block in librosa.stream(
        str(path),
        block_length=int(block_s * SAMPLE_RATE / HOP),
        frame_length=2048,
        hop_length=HOP,
        mono=True,
    ):
        mel = librosa.feature.melspectrogram(
            y=block, sr=SAMPLE_RATE, n_fft=2048, hop_length=HOP, n_mels=N_MELS, center=False
        )
        feats.append(np.log1p(mel * 1e3).T.astype(np.float32))
    frames = np.concatenate(feats)
    frames -= frames.mean(axis=0)
    frames /= frames.std(axis=0) + 1e-6
    return frames


def match_chunk(query: np.ndarray, vod: np.ndarray, vod_sq_cumsum: np.ndarray) -> tuple[int, float]:
    """Return (best frame offset, confidence) of query inside vod."""
    n = len(query)
    corr = sum(fftconvolve(vod[:, b], query[::-1, b], mode="valid") for b in range(N_MELS))
    window_energy = vod_sq_cumsum[n:] - vod_sq_cumsum[:-n]
    corr = corr / (np.sqrt(np.maximum(window_energy, 1e-6)) * np.linalg.norm(query))
    best = int(np.argmax(corr))
    confidence = float((corr[best] - corr.mean()) / (corr.std() + 1e-9))
    return best, confidence


def locate(query_path: str | Path, vod: np.ndarray, vod_sq_cumsum: np.ndarray) -> list[dict]:
    """Match every chunk of a query file; return chunks that resolved confidently."""
    query = mel_frames(query_path)
    chunk = int(CHUNK_S * SAMPLE_RATE / HOP)
    hits = []
    for start in range(0, max(len(query) - chunk + 1, 1), chunk):
        piece = query[start : start + chunk]
        if len(piece) < chunk // 2 or piece.std() < 0.1:
            continue
        offset, confidence = match_chunk(piece, vod, vod_sq_cumsum)
        if confidence >= MIN_CONFIDENCE:
            hits.append(
                {
                    "query_s": start * HOP / SAMPLE_RATE,
                    "vod_s": offset * HOP / SAMPLE_RATE,
                    "confidence": round(confidence, 1),
                }
            )
    return hits


def main() -> None:
    vod_path, out_path, *queries = sys.argv[1:]
    vod = mel_frames(vod_path)
    vod_sq_cumsum = np.concatenate([[0.0], np.cumsum((vod.astype(np.float64) ** 2).sum(axis=1))])
    results = {}
    for q in queries:
        hits = locate(q, vod, vod_sq_cumsum)
        results[Path(q).stem] = hits
        where = ", ".join(f"{h['vod_s'] / 60:.1f}m({h['confidence']})" for h in hits[:6])
        print(f"{Path(q).stem}: {len(hits)} chunks matched {where}")
    Path(out_path).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
