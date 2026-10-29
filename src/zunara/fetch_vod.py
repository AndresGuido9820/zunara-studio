"""Fetch everything the clipper needs for one Kick VOD, without downloading video.

Creates work/<channel>_<yyyymmdd>/ with:
    meta.json, base_url.txt   VOD metadata and HLS base URL
    subs_es.vtt               Kick auto-captions (the transcript, no ASR needed)
    audio16k.wav              16 kHz mono audio taken from the 160p rendition
    chat_samples.json         chat replay sampled every 30 s
    kick_clips/*.wav          audio of Kick clips made from this VOD
    ext_audio/*.wav           audio of recent third-party YouTube clips

Usage: zunara fetch CHANNEL --index 0 --workspace /path/to/work [--youtube-query TERMS]
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import requests

from zunara.config import Settings, safe_name
from zunara.media import executable

UA = {"User-Agent": "Mozilla/5.0"}
YT_EXCLUDE = ("w sound", "letra", "lyric", "video oficial", "official video", "ovy on")


def https_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Remote media must use an absolute HTTPS URL.")
    return value


def kick_get(path: str):
    response = requests.get(f"https://kick.com/api/{path}", headers=UA, timeout=20)
    response.raise_for_status()
    return response.json()


def ffmpeg_audio(url: str, out: Path) -> None:
    https_url(url)
    subprocess.run(
        [
            executable("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-user_agent",
            UA["User-Agent"],
            "-i",
            url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(out),
        ],
        check=True,
        timeout=3600,
    )  # nosec B603


def sample_chat(channel_id: int, start: datetime, duration_s: int, step: int = 30) -> list[dict]:
    rows, session = [], requests.Session()
    session.headers.update(UA)
    for offset in range(0, duration_s, step):
        t = (start + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            msgs = session.get(
                f"https://kick.com/api/v2/channels/{channel_id}/messages?start_time={t}", timeout=15
            ).json()["data"]["messages"]
        except (requests.RequestException, ValueError, KeyError):
            print("Chat sample unavailable; continuing with remaining samples.", file=sys.stderr)
            continue
        stamps = sorted(
            datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")) for m in msgs
        )
        span = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) > 1 else 0.0
        rows.append(
            {
                "offset": offset,
                "n": len(msgs),
                "span_s": span,
                "per_min": 60.0 * len(msgs) / span if span > 0 else None,
                "texts": [m["content"] for m in msgs][:40],
            }
        )
        time.sleep(0.2)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--workspace", type=Path, default=Settings.from_env().data_dir / "work")
    parser.add_argument("--youtube-query", action="append", default=[])
    args = parser.parse_args()
    channel = safe_name(args.channel)
    index = args.index
    if index < 0:
        raise ValueError("VOD index cannot be negative.")
    yt_terms = args.youtube_query

    vod = kick_get(f"v2/channels/{channel}/videos")[index]
    base = https_url(vod["source"]).rsplit("/", 1)[0]
    start = datetime.fromisoformat(vod["start_time"].replace(" ", "T") + "+00:00")
    duration_s = vod["duration"] // 1000
    work = args.workspace / f"{channel}_{start:%Y%m%d}"
    metadata = work / "meta.json"
    if metadata.exists() and json.loads(metadata.read_text())["livestream_id"] != vod["id"]:
        raise ValueError("Another VOD uses this workspace date; choose a different --workspace.")
    for sub in ("kick_clips", "ext_audio", "out"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    (work / "base_url.txt").write_text(base + "\n")
    (work / "meta.json").write_text(
        json.dumps(
            {
                "channel": channel,
                "channel_id": vod["channel_id"],
                "livestream_id": vod["id"],
                "title": vod["session_title"],
                "start_time": start.isoformat(),
                "duration_s": duration_s,
                "viewers": vod["viewer_count"],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"VOD: {vod['session_title']} ({start:%Y-%m-%d}, {duration_s / 3600:.1f} h) -> {work}")

    if not (work / "subs_es.vtt").exists():
        subprocess.run(
            [
                executable("yt-dlp"),
                "-q",
                "--no-warnings",
                "-o",
                str(work / "subs_es.vtt"),
                f"{base}/subtitles/es/playlist.m3u8",
            ],
            check=True,
            timeout=600,
        )  # nosec B603
    if not (work / "audio16k.wav").exists():
        ffmpeg_audio(f"{base}/160p30/playlist.m3u8", work / "audio16k.wav")
    print("subtítulos y audio listos")

    clips = {}
    for sort in ("view", "date"):
        for c in kick_get(f"v2/channels/{channel}/clips?sort={sort}&time=week").get("clips", []):
            if str(c["livestream_id"]) == str(vod["id"]):
                clips[c["id"]] = c
    (work / "kick_clips" / "vod_clips.json").write_text(
        json.dumps(list(clips.values()), ensure_ascii=False)
    )
    for c in clips.values():
        out = work / "kick_clips" / f"{safe_name(str(c['id']))}.wav"
        if not out.exists():
            ffmpeg_audio(c["clip_url"], out)
    print(f"{len(clips)} clips de Kick")

    ids = set()
    for term in yt_terms:
        url = f"https://www.youtube.com/results?search_query={quote_plus(term)}&sp=CAISAhgB"
        listing = subprocess.run(
            [executable("yt-dlp"), "--flat-playlist", "--print", "%(id)s|%(title)s", url],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout  # nosec B603
        listing = listing.splitlines()
        ids |= {
            ln.split("|")[0]
            for ln in listing
            if "|" in ln
            and len(ln.split("|")[0]) == 11
            and not any(x in ln.lower() for x in YT_EXCLUDE)
        }
    (work / "yt_ids.txt").write_text("\n".join(sorted(ids)) + "\n")
    subprocess.run(
        [
            executable("yt-dlp"),
            "-q",
            "--no-warnings",
            "--ignore-errors",
            "-f",
            "bestaudio[abr<=80]/bestaudio",
            "-x",
            "--audio-format",
            "wav",
            "--postprocessor-args",
            "ffmpeg:-ac 1 -ar 16000",
            "-o",
            str(work / "ext_audio" / "yt_%(id)s.%(ext)s"),
            "-a",
            str(work / "yt_ids.txt"),
        ],
        check=True,
        timeout=3600,
    )  # nosec B603
    print(f"{len(list((work / 'ext_audio').glob('*.wav')))} clips de terceros")

    if not (work / "chat_samples.json").exists():
        rows = sample_chat(vod["channel_id"], start, duration_s)
        (work / "chat_samples.json").write_text(json.dumps(rows, ensure_ascii=False))
    print("chat muestreado")


if __name__ == "__main__":
    main()
