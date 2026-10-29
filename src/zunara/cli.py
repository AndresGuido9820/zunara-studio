"""CLI boundary: parse inputs, call application services, present actionable errors."""

import argparse
import importlib
import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

from zunara import __version__, queue, youtube
from zunara.config import Settings
from zunara.media import probe


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
        command = commands.add_parser(name)
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
    for name, module in (
        ("fetch", "fetch_vod"),
        ("match", "audio_match"),
        ("rank", "rank_moments"),
    ):
        cmd = commands.add_parser(name, help=f"Run the {name} pipeline stage (--help for usage)")
        cmd.add_argument("arguments", nargs=argparse.REMAINDER)
        cmd.set_defaults(module=module)
    return root


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
        return {
            "exported": queue.export(
                args.work, args.destination or settings.data_dir / "ready", args.platform, only
            )
        }
    if args.command == "auth":
        return youtube.identity(youtube.client(settings, args.channel, interactive=True))
    if args.command == "publish":
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
        from zunara.render import render

        return {
            "file": str(render(args.work, args.start, args.end, args.hook, args.name, args.layout))
        }
    # Analysis dependencies and ASR models are loaded only for the stage that needs them.
    stage = importlib.import_module(f"zunara.{args.module}")
    previous = sys.argv
    try:
        sys.argv = [args.module, *args.arguments]
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
