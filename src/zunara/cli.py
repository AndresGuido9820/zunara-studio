"""CLI boundary: parse inputs, call application services, present actionable errors."""

import argparse
import contextlib
import importlib
import json
import math
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

from zunara import __version__, queue, youtube
from zunara.config import Settings
from zunara.editorial import load_policy
from zunara.media import probe
from zunara.storage import workspace_lock


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="zunara", description="Find, render, review and publish clips."
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local tools without reading tokens or calling APIs")
    inspect = commands.add_parser("inspect", help="Inspect a local video")
    inspect.add_argument("file", type=Path)
    for name in ("queue", "status", "approve", "export", "publish"):
        descriptions = {
            "queue": "Discover clips and refresh review hashes",
            "status": "Read the saved review queue",
            "approve": "Approve explicitly selected, reviewed clips",
            "export": "Export approved clips to a new local batch",
            "publish": "Preview or explicitly execute YouTube uploads",
        }
        command = commands.add_parser(name, help=descriptions[name])
        command.add_argument("work", type=Path)
        if name in {"approve", "export", "publish"}:
            command.add_argument("--only", help="Comma-separated clip stems")
        if name == "approve":
            command.set_defaults(require_only=True)
        if name == "export":
            command.add_argument(
                "--platform", required=True, choices=["tiktok", "instagram", "x", "youtube"]
            )
            command.add_argument("--destination", type=Path)
        if name == "publish":
            command.add_argument("--channel", required=True, help="Local OAuth token alias")
            command.add_argument("--channel-id", required=True, help="Exact YouTube UC… channel ID")
            command.add_argument(
                "--privacy", default="private", choices=["private", "unlisted", "public"]
            )
            command.add_argument("--schedule")
            command.add_argument("--every-hours", type=float, default=3)
            command.add_argument(
                "--execute", action="store_true", help="Perform remote uploads; default is preview"
            )
    auth = commands.add_parser("auth", help="Authorize a YouTube channel using desktop OAuth")
    auth.add_argument("--channel", required=True)
    render = commands.add_parser("render", help="Render from an existing workspace")
    render.add_argument("work", type=Path)
    render.add_argument("start", type=float)
    render.add_argument("end", type=float)
    render.add_argument("--hook", required=True)
    render.add_argument("--name", required=True)
    render.add_argument("--layout", choices=["4:3", "full"], default="4:3")
    render.add_argument(
        "--source",
        type=Path,
        metavar="PATH",
        help="Cut from this local video (e.g. a downloaded MP4) instead of the workspace VOD",
    )
    fetch = commands.add_parser("fetch", help="Acquire Kick VOD audio, subtitles and signals")
    fetch.add_argument("channel", help="Kick channel name")
    fetch.add_argument("--index", type=int, default=0, help="Nonnegative VOD index (default: 0)")
    fetch.add_argument("--workspace", type=Path, help="Parent of the channel/date workspace")
    fetch.add_argument(
        "--youtube-query", action="append", default=[], help="Search terms; repeatable"
    )
    match = commands.add_parser("match", help="Locate query audio inside VOD audio")
    match.add_argument("vod_audio", type=Path, help="Long 16 kHz mono WAV")
    match.add_argument("matches_json", type=Path, help="Output JSON path")
    match.add_argument("query_audio", type=Path, nargs="+", help="One or more query WAVs")
    rank = commands.add_parser("rank", help="Rank candidate moments in a VOD workspace")
    rank.add_argument("work", type=Path, help="Workspace containing analysis inputs")
    rank.add_argument("top_n", type=int, nargs="?", default=15, help="Positive count (default: 15)")
    return root


def require_workspace_files(work: Path, names: tuple[str, ...]) -> None:
    missing = [name for name in names if not (work / name).is_file()]
    if missing:
        raise ValueError(f"Missing workspace input(s): {', '.join(missing)}")


def run(args: argparse.Namespace) -> Any:
    settings = Settings.from_env()
    only = (
        {s.strip() for s in args.only.split(",") if s.strip()}
        if getattr(args, "only", None) is not None
        else None
    )
    if only is not None and not only:
        raise ValueError("--only must name at least one clip.")
    if args.command == "doctor":
        return {
            "version": __version__,
            "tools": {n: bool(shutil.which(n)) for n in ("ffmpeg", "ffprobe", "yt-dlp")},
            "config_dir": str(settings.config_dir),
            "data_dir": str(settings.data_dir),
            "youtube_client_configured": settings.client_secret.is_file(),
        }
    if args.command == "inspect":
        return probe(args.file)
    if args.command == "queue":
        return queue.refresh(args.work)
    if args.command == "status":
        return queue.load(args.work)
    if args.command == "approve":
        if not only:
            raise ValueError("Approval requires --only with the clips you have reviewed.")
        return queue.approve(args.work, only)
    if args.command == "export":
        policy = load_policy(settings.config_dir)
        # The topic policy is enforced at this boundary, before export creates any directory.
        with workspace_lock(args.work):
            policy.check(args.platform, queue.select(queue.load(args.work), only))
            exported = queue.export(
                args.work, args.destination or settings.data_dir / "ready", args.platform, only
            )
        return {"exported": exported}
    if args.command == "auth":
        return youtube.identity(youtube.client(settings, args.channel, interactive=True))
    if args.command == "publish":
        policy = load_policy(settings.config_dir)
        with workspace_lock(args.work):
            # Check before opening the client; rows that already have a video ID are skipped.
            pending = [
                r for r in queue.select(queue.load(args.work), only) if not r.get("youtube_id")
            ]
            policy.check("youtube", pending)
            return youtube.upload(
                args.work,
                youtube.client(settings, args.channel),
                channel_id=args.channel_id,
                only=only,
                privacy=args.privacy,
                schedule=args.schedule,
                every_hours=args.every_hours,
                execute=args.execute,
            )
    if args.command == "render":
        if (
            not all(math.isfinite(v) for v in (args.start, args.end))
            or args.start < 0
            or args.end <= args.start
        ):
            raise ValueError("Render interval must be finite and satisfy 0 <= start < end.")
        if args.source is None:
            require_workspace_files(args.work, ("audio16k.wav", "base_url.txt"))
        from zunara.render import render

        return {
            "file": str(
                render(
                    args.work, args.start, args.end, args.hook, args.name, args.layout, args.source
                )
            )
        }
    # Validate arguments before importing optional analysis dependencies or making requests.
    if args.command == "fetch":
        if args.index < 0:
            raise ValueError("VOD index cannot be negative.")
        module = "fetch_vod"
        arguments = [args.channel, "--index", str(args.index)]
        if args.workspace is not None:
            arguments += ["--workspace", str(args.workspace)]
        for term in args.youtube_query:
            arguments += ["--youtube-query", term]
    elif args.command == "match":
        module = "audio_match"
        arguments = [str(args.vod_audio), str(args.matches_json), *map(str, args.query_audio)]
    else:
        if args.top_n <= 0:
            raise ValueError("TOP_N must be greater than zero.")
        require_workspace_files(
            args.work, ("audio16k.wav", "chat_samples.json", "subs_es.vtt", "matches.json")
        )
        module = "rank_moments"
        arguments = [str(args.work), str(args.top_n)]
    stage = importlib.import_module(f"zunara.{module}")
    previous = sys.argv
    try:
        sys.argv = [module, *arguments]
        # Keep stdout parseable for callers; stage progress is diagnostic output.
        with contextlib.redirect_stdout(sys.stderr):
            stage.main()
    finally:
        sys.argv = previous
    return {"stage": args.command, "status": "complete"}


def main(argv: list[str] | None = None) -> None:
    root = parser()
    args = root.parse_args(argv)
    try:
        result = run(args)
    except (ValueError, OSError, KeyError, ImportError, subprocess.SubprocessError) as error:
        # Remote/ffmpeg exceptions may include OAuth tokens or signed source URLs: don't dump them.
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Operation failed; check input files, dependencies and access."
        )
        root.exit(2, f"zunara: {message}\n")
    except Exception:
        # SDK errors can contain credentials or signed URLs; never render their payloads.
        root.exit(2, "zunara: Remote or model operation failed; check access and dependencies.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
