"""Rank the moments of a VOD by combining every cheap signal.

Signals (all per second of VOD):
  * picks   - how many independent clips (Kick clips, third-party channels)
              cover that second, found by audio matching;
  * chat    - chat messages per minute against a rolling baseline;
  * loud    - audio loudness against a rolling baseline.

Usage: python3 rank_moments.py <work_dir> [top_n]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from zunara.signals import (
    chat_per_second,
    loudness_per_second,
    parse_rolling_vtt,
    smooth,
    transcript_window,
    zscore,
)

MIN_CONFIDENCE = 5.5
ALIGN_TOLERANCE_S = 2.0
WEIGHTS = {"picks": 1.0, "chat": 0.35, "loud": 0.25}


def matched_segments(hits: list[dict]) -> list[tuple[float, float]]:
    """Turn chunk hits into VOD segments backed by >=2 aligned chunks."""
    hits = [h for h in hits if h["confidence"] >= MIN_CONFIDENCE]
    hits.sort(key=lambda h: h["query_s"])
    segments, run = [], []
    for h in hits:
        shift = h["vod_s"] - h["query_s"]
        if run and abs(shift - (run[-1]["vod_s"] - run[-1]["query_s"])) <= ALIGN_TOLERANCE_S:
            run.append(h)
            continue
        if len(run) >= 2:
            segments.append((run[0]["vod_s"], run[-1]["vod_s"] + 8.0))
        run = [h]
    if len(run) >= 2:
        segments.append((run[0]["vod_s"], run[-1]["vod_s"] + 8.0))
    return segments


def main() -> None:
    work = Path(sys.argv[1])
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    loud = loudness_per_second(work / "audio16k.wav")
    duration = len(loud)
    chat = chat_per_second(work / "chat_samples.json", duration)
    phrases = parse_rolling_vtt(work / "subs_es.vtt")

    picks = np.zeros(duration)
    sources = [[] for _ in range(duration)]
    matches = json.loads((work / "matches.json").read_text())
    for name, hits in matches.items():
        for start, end in matched_segments(hits):
            # Long re-uploads cover everything; they say little about what is good.
            if end - start > 300:
                continue
            a, b = max(0, int(start)), min(int(end), duration)
            picks[a:b] += 1
            for s in range(a, b):
                sources[s].append(name)

    score = (
        WEIGHTS["picks"] * smooth(picks, 15)
        + WEIGHTS["chat"] * np.clip(zscore(smooth(chat, 30)), 0, None)
        + WEIGHTS["loud"] * np.clip(zscore(smooth(loud, 5)), 0, None)
    )

    moments, taken = [], np.zeros(duration, dtype=bool)
    for peak in np.argsort(score)[::-1]:
        if taken[peak] or len(moments) >= top_n:
            continue
        start, end = max(peak - 35, 0), min(peak + 35, duration)
        taken[max(start - 30, 0) : end + 30] = True
        moments.append(
            {
                "peak_s": int(peak),
                "start_s": int(start),
                "end_s": int(end),
                "score": round(float(score[peak]), 2),
                "picks": int(picks[start:end].max()),
                "sources": sorted({s for sec in sources[start:end] for s in sec}),
                "chat_per_min": round(float(chat[start:end].max())),
                "loud_db": round(float(loud[start:end].max()), 1),
                "transcript": transcript_window(phrases, start, end),
            }
        )
    (work / "moments.json").write_text(json.dumps(moments, ensure_ascii=False, indent=1))
    for m in moments:
        print(
            f"{m['peak_s'] // 60:3d}:{m['peak_s'] % 60:02d} score={m['score']:5.2f} "
            f"picks={m['picks']} chat={m['chat_per_min']}/min "
            f"src={len(m['sources'])} | {m['transcript'][:110]}"
        )


if __name__ == "__main__":
    main()
