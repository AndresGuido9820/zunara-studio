"""CLI boundary: parse inputs, call application services, present actionable errors."""

import argparse
import contextlib
import importlib
import json
import math
import shutil
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zunara import (
    __version__,
    browser,
    browser_bridge,
    instagram,
    queue,
    scheduler,
    social,
    youtube,
)
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
    render = commands.add_parser("render", help="Render a local video or workspace VOD interval")
    render.add_argument("work", type=Path)
    render.add_argument("start", type=float, help="Start time in seconds on the source timeline")
    render.add_argument("end", type=float, help="Exclusive end time in seconds")
    render.add_argument("--hook", required=True, help="Title burned into the top of the frame")
    render.add_argument("--name", required=True)
    render.add_argument(
        "--layout",
        choices=["4:3", "full"],
        default="4:3",
        help="Foreground: center 4:3 crop (default) or full wide frame",
    )
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
    social_parser = commands.add_parser("social", help="Publish through the Upload-Post API")
    actions = social_parser.add_subparsers(dest="social_command", required=True)
    actions.add_parser("doctor", help="Check API key configuration offline")
    actions.add_parser("accounts", help="List provider profiles and connected account IDs")
    post = actions.add_parser("publish", help="Preview or explicitly execute API uploads")
    post.add_argument("work", type=Path)
    post.add_argument("--only", required=True, help="Comma-separated clip stems")
    post.add_argument("--profile", required=True, help="Upload-Post profile username")
    post.add_argument(
        "--platform", action="append", required=True, choices=social.PLATFORMS, help="Repeatable"
    )
    post.add_argument(
        "--account",
        action="append",
        default=[],
        metavar="PLATFORM=ID",
        help="Exact connected account username or handle; one per platform",
    )
    post.add_argument("--schedule", help="Future ISO datetime with a UTC offset")
    post.add_argument("--language", help="YouTube metadata language (BCP-47); omitted by default")
    post.add_argument(
        "--privacy",
        default="private",
        choices=["private", "unlisted", "public"],
        help="YouTube privacy (default: private)",
    )
    post.add_argument(
        "--execute", action="store_true", help="Perform remote uploads; default is preview"
    )
    social_status = actions.add_parser("status", help="Read API publication receipts")
    social_status.add_argument("work", type=Path)
    social_status.add_argument(
        "--refresh", action="store_true", help="Query provider status for saved request IDs"
    )
    native = commands.add_parser("instagram", help="Publish Reels natively via the Graph API")
    steps = native.add_subparsers(dest="instagram_command", required=True)
    steps.add_parser("doctor", help="Check access token configuration offline")
    account = steps.add_parser("account", help="Verify the token's Instagram account (GET only)")
    for name in ("publish", "finish"):
        helps = {
            "publish": "Preview, or explicitly create and upload Reels containers",
            "finish": "Preview (GET only), or explicitly publish containers that finished processing",
        }
        step = steps.add_parser(name, help=helps[name])
        step.add_argument("work", type=Path)
        step.add_argument("--only", required=True, help="Comma-separated clip stems")
    for step in (account, steps.choices["publish"], steps.choices["finish"]):
        step.add_argument("--account-id", required=True, help="Numeric Instagram account ID")
        step.add_argument("--username", required=True, help="Exact Instagram username")
        step.add_argument("--api-version", default=instagram.API_VERSION)
    for name in ("publish", "finish"):
        steps.choices[name].add_argument(
            "--execute", action="store_true", help="Perform remote writes; default is preview"
        )
    steps.choices["publish"].add_argument(
        "--ai-generated", action="store_true", help="Disclose AI-generated content to Instagram"
    )
    native_status = steps.add_parser("status", help="Read Instagram receipts")
    native_status.add_argument("work", type=Path)
    native_status.add_argument(
        "--refresh", action="store_true", help="Read container status (GET only)"
    )
    unattended = commands.add_parser(
        "browser", help="Publish Instagram posts through a dedicated, unattended browser profile"
    )
    tasks = unattended.add_subparsers(dest="browser_command", required=True)
    tasks.add_parser("doctor", help="Check Playwright, Chromium and profiles; opens no browser")
    connect = tasks.add_parser(
        "login",
        help="Dedicated profile only: open a headed window on it for a manual login",
    )
    bridge = tasks.add_parser(
        "connect",
        help="Main Chrome: attach the Playwright Extension and verify the logged-in account",
    )
    bridge.add_argument(
        "--timeout",
        type=float,
        default=browser_bridge.ATTACH_TIMEOUT,
        help="Seconds to wait for your approval in Chrome (default: 120)",
    )
    tasks.add_parser("disconnect", help="Detach from main Chrome; Chrome keeps running")
    run_post = tasks.add_parser(
        "publish", help="Preview offline, or explicitly publish approved clips in the browser"
    )
    run_post.add_argument("work", type=Path)
    run_post.add_argument("--only", required=True, help="Comma-separated clip stems")
    for task in (connect, bridge, run_post):
        task.add_argument(
            "--platform", required=True, help="Destination; only instagram is supported"
        )
        task.add_argument("--username", required=True, help="Exact Instagram username")
    connect.add_argument(
        "--timeout",
        type=float,
        default=browser.LOGIN_TIMEOUT,
        help=f"Seconds to wait for the manual login (default: {browser.LOGIN_TIMEOUT:g})",
    )
    run_post.add_argument(
        "--execute", action="store_true", help="Open the browser and publish; default is preview"
    )
    run_post.add_argument(
        "--connection",
        choices=browser.CONNECTIONS,
        default="dedicated",
        help="dedicated: Zunara's own profile (default); chrome: your main Chrome via the bridge",
    )
    run_post.add_argument(
        "--headed",
        action="store_true",
        help="Dedicated profile only: show the window (default: headless)",
    )
    run_post.add_argument(
        "--share-timeout",
        type=float,
        default=browser.SHARE_TIMEOUT,
        help="Seconds to wait for Instagram's shared confirmation after clicking Share",
    )
    run_post.add_argument(
        "--retry-unshared",
        action="store_true",
        help="Allow a clip whose media was attached but never shared (check drafts first)",
    )
    browser_status = tasks.add_parser("status", help="Read browser publication receipts")
    browser_status.add_argument("work", type=Path)
    jobs = commands.add_parser("jobs", help="Schedule approved clips and run due jobs locally")
    job_steps = jobs.add_subparsers(dest="jobs_command", required=True)
    enqueue = job_steps.add_parser("add", help="Schedule one approved clip (offline)")
    enqueue.add_argument("work", type=Path)
    enqueue.add_argument("--only", required=True, help="One clip stem")
    enqueue.add_argument("--destination", required=True, choices=scheduler.DESTINATIONS)
    enqueue.add_argument("--at", required=True, help="ISO datetime with a UTC offset")
    enqueue.add_argument("--channel", help="youtube: local OAuth token alias")
    enqueue.add_argument("--channel-id", help="youtube: exact UC… channel ID")
    enqueue.add_argument(
        "--privacy", choices=["private", "unlisted", "public"], help="youtube (default: private)"
    )
    enqueue.add_argument("--account-id", help="instagram: numeric account ID")
    enqueue.add_argument("--username", help="instagram, instagram-browser: exact username")
    enqueue.add_argument("--ai-generated", action="store_true", help="instagram: AI disclosure")
    enqueue.add_argument(
        "--connection", choices=browser.CONNECTIONS, help="instagram-browser (default: dedicated)"
    )
    job_steps.add_parser("list", help="Show jobs, saved results and setup hints (offline)")
    stop = job_steps.add_parser("cancel", help="Cancel a pending or blocked job")
    stop.add_argument("job_id")
    due = job_steps.add_parser("run", help="Preview due jobs offline, or --execute them")
    due.add_argument("--execute", action="store_true", help="Run due jobs; default is preview")
    due.add_argument(
        "--max-per-platform-day",
        type=int,
        default=scheduler.MAX_PER_DAY,
        help=f"Distinct clips started per platform and day (default: {scheduler.MAX_PER_DAY})",
    )
    due.add_argument("--timezone", default="UTC", help="IANA zone for the daily cap (default: UTC)")
    return root


def run_jobs(args: argparse.Namespace, settings: Settings) -> Any:
    if args.jobs_command == "list":
        return scheduler.listing(settings)
    if args.jobs_command == "cancel":
        return scheduler.cancel(settings, args.job_id)
    if args.jobs_command == "run":
        # Jobs go through the regular commands, so locks, topic policy and receipts all apply.
        return scheduler.run(
            settings,
            lambda argv: run(parser().parse_args(argv)),
            execute=args.execute,
            max_per_day=args.max_per_platform_day,
            timezone=args.timezone,
        )
    if "," in args.only:
        raise ValueError("A job takes exactly one clip; add one job per clip.")
    options = {
        k: getattr(args, k)
        for k in ("channel", "channel_id", "privacy", "account_id", "username", "connection")
    }
    options["ai_generated"] = args.ai_generated
    return scheduler.add(settings, args.work, args.only.strip(), args.destination, args.at, options)


def run_browser(args: argparse.Namespace, settings: Settings, only: set[str] | None) -> Any:
    command = args.browser_command
    if command == "doctor":
        return {
            **browser.doctor(settings.config_dir),
            "chrome_bridge": browser_bridge.doctor(settings.config_dir),
        }
    if command == "disconnect":
        return browser_bridge.disconnect(settings.config_dir)
    if command == "status":
        with workspace_lock(args.work):
            return browser.status(args.work)
    browser.check_platform(args.platform)
    username = browser.check_username(args.username)
    if command == "login":
        timeout = browser.check_timeout(args.timeout, "--timeout")
        return browser.login(settings.config_dir, username, timeout)
    if command == "connect":
        timeout = browser.check_timeout(args.timeout, "--timeout")
        return browser_bridge.connect(settings.config_dir, username, timeout)
    if args.connection == "chrome" and args.headed:
        raise ValueError("--headed applies only to the dedicated profile.")
    share_timeout = browser.check_timeout(args.share_timeout, "--share-timeout")
    policy = load_policy(settings.config_dir)
    with workspace_lock(args.work):
        items = browser.plan(args.work, only or set(), username, retry_unshared=args.retry_unshared)
        # The topic policy is enforced before any browser is opened.
        policy.check("instagram", [i["_row"] for i in items if i["action"] == "publish"])
        if not args.execute:
            return browser.preview(settings.config_dir, args.work, items, username, args.connection)
        return browser.execute(
            args.work,
            settings.config_dir,
            items,
            username=username,
            headed=args.headed,
            share_timeout=share_timeout,
            connection=args.connection,
        )


def require_workspace_files(work: Path, names: tuple[str, ...]) -> None:
    missing = [name for name in names if not (work / name).is_file()]
    if missing:
        raise ValueError(f"Missing workspace input(s): {', '.join(missing)}")


def run_social(args: argparse.Namespace, settings: Settings, only: set[str] | None) -> Any:
    if args.social_command == "doctor":
        return {
            **social.key_status(settings.config_dir),
            "api": social.API,
            "live_auth": "unknown; doctor sends no requests",
            "setup": social.SETUP,
        }
    if args.social_command == "accounts":
        return social.accounts(social.Client(social.load_key(settings.config_dir)))
    if args.social_command == "status":
        with workspace_lock(args.work):
            client = social.Client(social.load_key(settings.config_dir)) if args.refresh else None
            return social.status(args.work, client)
    if len(set(args.platform)) != len(args.platform):
        raise ValueError("--platform lists a platform more than once.")
    expected = social.parse_accounts(args.account, args.platform)
    schedule = social.parse_schedule(args.schedule, datetime.now(UTC))
    policy = load_policy(settings.config_dir)
    with workspace_lock(args.work):
        items = social.plan(args.work, only or set(), args.platform, args.profile, expected)
        uploads = [i for i in items if i["action"] == "upload"]
        for platform in args.platform:
            # The topic policy is enforced per destination before the key is read or sent.
            policy.check(platform, [i["_row"] for i in uploads if i["platform"] == platform])
        if not uploads:
            public = [{k: v for k, v in i.items() if k != "_row"} for i in items]
            return {"mode": "nothing-to-do", "items": public}
        return social.publish(
            args.work,
            social.Client(social.load_key(settings.config_dir)),
            items,
            profile=args.profile,
            expected=expected,
            schedule=schedule,
            privacy=args.privacy,
            language=args.language,
            execute=args.execute,
        )


def run_instagram(args: argparse.Namespace, settings: Settings, only: set[str] | None) -> Any:
    command = args.instagram_command
    if command == "doctor":
        return {
            **instagram.token_status(settings.config_dir),
            "hosts": [instagram.GRAPH, instagram.UPLOAD],
            "api_version": instagram.API_VERSION,
            "live_auth": "unknown; doctor sends no requests",
        }
    if command == "status":
        with workspace_lock(args.work):
            token = instagram.load_token(settings.config_dir) if args.refresh else None
            return instagram.status(args.work, instagram.Client(token) if token else None)
    account_id, username = instagram.check_account(args.account_id, args.username)
    version = instagram.check_version(args.api_version)
    if command == "account":
        reader = instagram.Client(instagram.load_token(settings.config_dir))
        return instagram.identity(reader, version, account_id, username)
    policy = load_policy(settings.config_dir)
    with workspace_lock(args.work):
        if command == "publish":
            items = instagram.plan(
                args.work, only or set(), account_id, username, args.ai_generated
            )
            action = "create"
        else:
            items = instagram.finish_plan(args.work, only or set(), account_id, username)
            action = "check"  # Containers that may become publishable.
        # The topic policy is enforced before the token is read or any request is sent.
        policy.check("instagram", [i["_row"] for i in items if i["action"] == action])
        if command == "finish":
            return instagram.finish(
                args.work,
                instagram.Client(instagram.load_token(settings.config_dir)),
                items,
                account_id=account_id,
                username=username,
                version=version,
                execute=args.execute,
            )
        return instagram.publish(
            args.work,
            instagram.Client(instagram.load_token(settings.config_dir)),
            items,
            account_id=account_id,
            username=username,
            version=version,
            execute=args.execute,
        )


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
    if args.command == "social":
        return run_social(args, settings, only)
    if args.command == "instagram":
        return run_instagram(args, settings, only)
    if args.command == "browser":
        return run_browser(args, settings, only)
    if args.command == "jobs":
        return run_jobs(args, settings)
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
