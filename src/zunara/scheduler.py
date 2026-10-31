"""Local publishing scheduler: durable jobs that call the existing adapters when they are due.

Jobs live in ``DATA_DIR/scheduler/jobs.json`` (private, versioned). A job freezes the approved
media hash, its metadata and an immutable destination identity at enqueue time. ``run`` previews
offline by default; ``run --execute`` calls the regular CLI commands, so workspace locks, topic
policy and every adapter receipt guard still apply. The adapters' own receipts decide the outcome:

* ``blocked``: provably nothing was sent (setup missing, metadata changed); tried again next run.
* ``waiting``: a native Instagram container is processing; the next run reads it with a GET and
  publishes it once Meta reports FINISHED. It is never recreated.
* ``unknown`` / ``interrupted``: a remote write may have happened. Never retried automatically.
  A crashed running job can resume only when its adapter receipt proves a safe continuation.
* ``succeeded`` / ``failed`` / ``skipped`` / ``cancelled``: final.

There is no daemon. Run ``zunara jobs run --execute`` from cron or a systemd timer.
"""

import json
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from filelock import FileLock, Timeout

from zunara import browser, instagram, queue, social
from zunara.config import Settings, safe_name
from zunara.editorial import load_policy
from zunara.storage import workspace_lock, write_json

VERSION = 1
FOLDER = "scheduler"
JOBS = "jobs.json"
DESTINATIONS = ("youtube", "instagram", "instagram-browser", "tiktok")
STATES = (
    "pending",
    "running",
    "waiting",
    "blocked",
    "unknown",
    "interrupted",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
)
RUNNABLE = {"pending", "blocked", "waiting"}
CANCELLABLE = {"pending", "blocked"}
# States in which another job for the same media and account must not run.
OCCUPIED = {"running", "waiting", "unknown", "interrupted", "succeeded"}
# States whose job sent (or may have sent) a write; they count toward the daily cap.
WROTE = {"running", "waiting", "unknown", "interrupted", "succeeded", "failed"}
MAX_PER_DAY = 2
CHECK_INTERVAL = timedelta(seconds=60)  # Meta suggests reading processing status once a minute.
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
JOB_ID = re.compile(r"[0-9a-f]{12}")
FROZEN = ("title", "description", "tags", "topic")
TIKTOK = (
    "Native TikTok publishing is not implemented. It needs your own TikTok developer app with an "
    "approved video.publish scope and audit for public posts; see docs/guides/native-publishing.md."
)
SETUP = {
    "youtube": "Check 'zunara doctor' and authorize once with 'zunara auth --channel ALIAS'.",
    "instagram": "Check 'zunara instagram doctor' and 'zunara instagram account ...'.",
    "instagram-browser": "Check 'zunara browser doctor'; log in or connect the browser yourself.",
}

Runner = Callable[[list[str]], Any]


def _now() -> datetime:
    return datetime.now(UTC)


def _folder(settings: Settings) -> Path:
    folder = settings.data_dir / FOLDER
    if folder.is_symlink():
        raise ValueError("The scheduler directory must not be a symlink.")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    return folder


@contextmanager
def jobs_lock(settings: Settings) -> Iterator[Path]:
    folder = _folder(settings)
    try:
        with FileLock(str(folder / ".jobs.lock"), timeout=10):
            yield folder / JOBS
    except Timeout as error:
        raise ValueError("The jobs file is busy; try again.") from error


@contextmanager
def scheduler_lock(settings: Settings) -> Iterator[None]:
    try:
        with FileLock(str(_folder(settings) / ".scheduler.lock"), timeout=0):
            yield
    except Timeout as error:
        raise ValueError(
            "Another 'zunara jobs run --execute' is active; nothing was run."
        ) from error


def load(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"{JOBS} must not be a symlink.")
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError(f"{JOBS} has an unknown format or version.")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or any(
        not isinstance(j, dict)
        or not JOB_ID.fullmatch(str(j.get("id")))
        or j.get("state") not in STATES
        or j.get("destination") not in DESTINATIONS[:3]
        or not isinstance(j.get("identity"), dict)
        or not isinstance(j.get("frozen"), dict)
        for j in jobs
    ):
        raise ValueError(f"{JOBS} contains an invalid job.")
    if len({j["id"] for j in jobs}) != len(jobs):
        raise ValueError(f"{JOBS} contains a duplicate job ID.")
    return jobs


def save(path: Path, jobs: list[dict[str, Any]]) -> None:
    write_json(path, {"version": VERSION, "jobs": jobs}, private=True)


def parse_at(value: str, now: datetime) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("--at must be an ISO datetime, e.g. 2030-01-01T18:00:00-05:00.") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("--at needs an explicit UTC offset, e.g. 2030-01-01T18:00:00-05:00.")
    if moment <= now:
        raise ValueError("--at must be in the future.")
    return moment


def identity(destination: str, options: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the exact adapter identity; it never changes after enqueue."""
    if destination == "tiktok":
        raise ValueError(TIKTOK)
    if destination == "youtube":
        if not options.get("channel") or not options.get("channel_id"):
            raise ValueError("youtube jobs need --channel ALIAS and --channel-id UC….")
        if not CHANNEL_ID.fullmatch(options["channel_id"]):
            raise ValueError("--channel-id must be the exact UC… YouTube channel ID.")
        return {"channel": safe_name(options["channel"]), "channel_id": options["channel_id"]}
    if not options.get("username"):
        raise ValueError(f"{destination} jobs need --username.")
    if destination == "instagram":
        if not options.get("account_id"):
            raise ValueError("instagram jobs need --account-id and --username.")
        account_id, username = instagram.check_account(options["account_id"], options["username"])
        return {"account_id": account_id, "username": username}
    connection = options.get("connection") or "dedicated"
    if connection not in browser.CONNECTIONS:
        raise ValueError("--connection must be dedicated or chrome.")
    return {"username": browser.check_username(options["username"]), "connection": connection}


def _key(job: dict[str, Any]) -> tuple[str, str, str]:
    """Same media to the same account, across workspaces and Instagram routes."""
    who = job["identity"]
    if job["destination"] == "youtube":
        return ("youtube", who["channel_id"], job["frozen"]["approved_sha256"])
    return ("instagram", who["username"].lower(), job["frozen"]["approved_sha256"])


def _platform(destination: str) -> str:
    return "youtube" if destination == "youtube" else "instagram"


def _row(work: Path, file: str) -> dict[str, Any]:
    for row in queue.load(work):
        if row["file"] == file:
            return row
    raise ValueError(f"{file} is no longer in the workspace queue.")


def add(
    settings: Settings,
    work: Path,
    stem: str,
    destination: str,
    at: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Enqueue one approved clip. Offline: reads the workspace and policy, contacts nothing."""
    who = identity(destination, options)
    now = _now()
    moment = parse_at(at, now)
    work = work.expanduser().resolve()
    privacy = options.get("privacy") or "private"
    if destination != "youtube" and options.get("privacy"):
        raise ValueError("--privacy applies only to youtube jobs.")
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("--privacy must be private, unlisted or public.")
    with workspace_lock(work):
        rows = queue.select(queue.load(work), {stem})
        row = rows[0]
        queue.verify_approved(work, row)
        if destination != "youtube":
            instagram.caption(row)
        load_policy(settings.config_dir).check(_platform(destination), rows)
    frozen = {k: row.get(k) for k in FROZEN}
    frozen.update(approved_sha256=row["approved_sha256"])
    if options.get("ai_generated") and destination != "instagram":
        # Only the native adapter can send the disclosure; never drop it silently.
        raise ValueError("--ai-generated is supported only for native instagram jobs.")
    if destination == "youtube":
        frozen["privacy"] = privacy
    else:
        frozen["ai_generated"] = bool(options.get("ai_generated"))
    job: dict[str, Any] = {
        "id": secrets.token_hex(6),
        "workspace": str(work),
        "file": row["file"],
        "destination": destination,
        "identity": who,
        "frozen": frozen,
        "at": moment.isoformat(),
        "at_utc": moment.astimezone(UTC).isoformat(),
        "state": "pending",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "started_at": None,
        "checked_at": None,
        "result": None,
        "error": None,
    }
    with jobs_lock(settings) as path:
        jobs = load(path)
        clash = [
            j["id"]
            for j in jobs
            if _key(j) == _key(job) and j["state"] not in {"cancelled", "failed", "skipped"}
        ]
        if clash:
            raise ValueError(f"A job for the same media and account already exists: {clash[0]}.")
        jobs.append(job)
        save(path, jobs)
    return job


def listing(settings: Settings) -> dict[str, Any]:
    with jobs_lock(settings) as path:
        jobs = load(path)
    used = sorted({j["destination"] for j in jobs})
    return {"jobs": jobs, "setup": {d: SETUP[d] for d in used}}


def check_cap(max_per_day: int, timezone: str) -> ZoneInfo:
    if not 1 <= max_per_day <= 50:
        raise ValueError("--max-per-platform-day must be between 1 and 50.")
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("--timezone must be an IANA name, e.g. America/Bogota or UTC.") from None


def _cap_reached(
    settings: Settings, job: dict[str, Any], now: datetime, cap: tuple[int, ZoneInfo]
) -> bool:
    """Distinct clips with a started write on this local day, per platform (both IG routes)."""
    limit, zone = cap
    today = now.astimezone(zone).date()
    with jobs_lock(settings) as path:
        jobs = load(path)
    started = {
        j["frozen"]["approved_sha256"]
        for j in jobs
        if _platform(j["destination"]) == _platform(job["destination"])
        and j["state"] in WROTE
        and j.get("started_at")
        and datetime.fromisoformat(j["started_at"]).astimezone(zone).date() == today
    }
    return job["frozen"]["approved_sha256"] not in started and len(started) >= limit


def cancel(settings: Settings, job_id: str) -> dict[str, Any]:
    with jobs_lock(settings) as path:
        jobs = load(path)
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        if job["state"] not in CANCELLABLE:
            raise ValueError(
                f"Job {job_id} is {job['state']}; only pending or blocked jobs can be cancelled."
            )
        job.update(state="cancelled", updated_at=_now().isoformat())
        save(path, jobs)
        return job


def _revalidate(job: dict[str, Any]) -> dict[str, Any]:
    """Offline: the queue row must still carry the approved bytes and metadata frozen at enqueue."""
    work = Path(job["workspace"])
    row = _row(work, job["file"])
    frozen = job["frozen"]
    if row.get("approved_sha256") != frozen["approved_sha256"]:
        raise ValueError("Approval changed since the job was added; cancel it and add a new job.")
    queue.verify_approved(work, row)
    changed = [k for k in FROZEN if row.get(k) != frozen.get(k)]
    if changed:
        raise ValueError(
            f"Queue {', '.join(changed)} changed since the job was added; restore it, or cancel "
            "the job and add a new one."
        )
    return row


def _native(job: dict[str, Any]) -> dict[str, Any] | None:
    who = job["identity"]
    receipts = instagram.load_ledger(Path(job["workspace"]))["records"]
    for record in receipts:
        if (record["account_id"], record["approved_sha256"]) == (
            who["account_id"],
            job["frozen"]["approved_sha256"],
        ):
            if record.get("username") != who["username"]:
                raise ValueError("The Instagram receipt for this clip names another username.")
            return dict(record)
    return None


def _browser(job: dict[str, Any]) -> dict[str, Any] | None:
    for record in browser.load_ledger(Path(job["workspace"]))["records"]:
        if record["platform"] == "instagram" and (
            record["username"],
            record["approved_sha256"],
        ) == (
            job["identity"]["username"],
            job["frozen"]["approved_sha256"],
        ):
            return dict(record)
    return None


def _youtube_prior(work: Path, row: dict[str, Any]) -> str | None:
    """Manual or provider YouTube receipts for these bytes; ``youtube.upload`` ignores them.

    Provider accounts cannot be mapped to a channel ID, so any non-failed record holds the clip.
    An unresolved manual record raises, which blocks the job without uploading.
    """
    single = len(queue.load(work)) == 1
    prior = social._prior_outcome(social._prior_entries(work), row, "youtube", single)
    if prior:
        return f"publications.json records {prior}"
    for record in social.load_ledger(work)["records"]:
        if (
            record["platform"] == "youtube"
            and record["approved_sha256"] == row["approved_sha256"]
            and record["state"] != "failed"
        ):
            return f"{social.LEDGER} records {record['state']}"
    return None


def outcome(job: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Classify from the adapter's own receipt; ``None`` means provably nothing was sent."""
    destination = job["destination"]
    if destination == "youtube":
        row = _row(Path(job["workspace"]), job["file"])
        if row.get("youtube_id") and row.get("channel_id") != job["identity"]["channel_id"]:
            # The adapter never uploads a row twice, so this job cannot publish this clip.
            return "skipped", {
                "reason": "the queue row was uploaded to another or unrecorded channel"
            }
        if row.get("youtube_id"):
            video = row["youtube_id"]
            return "succeeded", {
                "video_id": video,
                "url": f"https://www.youtube.com/watch?v={video}",
                "privacy_requested": job["frozen"]["privacy"],
                "visibility": "not verified; YouTube may still be processing or restrict it",
            }
        if row.get("upload_state") == "uploading":
            return "unknown", {"reason": "upload started; reconcile in YouTube Studio"}
        prior = _youtube_prior(Path(job["workspace"]), row)
        return ("skipped", {"reason": prior}) if prior else None
    if destination == "instagram":
        record = _native(job)
        if record is None:
            return None
        state = record["state"]
        result = {
            k: record.get(k)
            for k in ("state", "container_id", "media_id", "permalink", "status_code")
        }
        if state in {"published", "api_published"}:
            return "succeeded", result
        if state in {"created", "uploading", "processing", "ready"}:
            return "waiting", result
        if state in {"failed", "expired"}:
            return "failed", result
        return "unknown", result  # creating, unknown, publishing: never recreated or resent.
    record = _browser(job)
    if record is None:
        return None
    result = {k: record.get(k) for k in ("state", "status", "permalink", "connection")}
    state = record["state"]
    if state == "published":
        return "succeeded", result
    if state in {"sharing", "unknown", "composing"} or (
        state == "stopped" and record["media_selected"]
    ):
        return "unknown", result
    return None  # preflight, or stopped before any media was attached.


def _argv(job: dict[str, Any], step: str) -> list[str]:
    who, work, stem = job["identity"], job["workspace"], Path(job["file"]).stem
    if job["destination"] == "youtube":
        return [
            "publish", work, "--only", stem, "--channel", who["channel"],
            "--channel-id", who["channel_id"], "--privacy", job["frozen"]["privacy"], "--execute",
        ]  # fmt: skip
    if job["destination"] == "instagram":
        argv = [
            "instagram", step, work, "--only", stem,
            "--account-id", who["account_id"], "--username", who["username"], "--execute",
        ]  # fmt: skip
        return argv + (
            ["--ai-generated"] if step == "publish" and job["frozen"]["ai_generated"] else []
        )
    return [
        "browser", "publish", work, "--only", stem, "--platform", "instagram",
        "--username", who["username"], "--connection", who["connection"], "--execute",
    ]  # fmt: skip


def _step(job: dict[str, Any]) -> tuple[str, str | None]:
    """Next action from receipts, offline: (step, reason). Step ``none`` sends nothing."""
    found = outcome(job)
    if job["destination"] == "instagram" and found and found[0] == "waiting":
        return "finish", None
    if found:
        return "none", f"receipt already records {found[1].get('state') or found[0]}"
    if job["destination"] == "youtube":
        return "upload", None
    return ("publish", None) if job["destination"] == "instagram" else ("browser", None)


def _skip_reason(result: Any) -> str | None:
    items = result.get("items") if isinstance(result, dict) else result
    if not isinstance(items, list):
        return None
    reasons = [i.get("reason") for i in items if isinstance(i, dict) and i.get("action") == "skip"]
    return str(reasons[0]) if reasons else None


def _message(error: BaseException) -> str:
    # Adapter ValueErrors are written to be shown; anything else may echo SDK payloads.
    if isinstance(error, ValueError):
        return str(error)
    return "Operation failed; check setup, dependencies and access."


def _update(settings: Settings, job_id: str, expect: set[str], **fields: Any) -> dict[str, Any]:
    """Change a job only while its saved state is still in ``expect`` (e.g. not cancelled)."""
    with jobs_lock(settings) as path:
        jobs = load(path)
        job = next(j for j in jobs if j["id"] == job_id)
        if job["state"] not in expect:
            return job
        job.update(updated_at=_now().isoformat(), **fields)
        save(path, jobs)
        return job


def _run_one(
    settings: Settings, job: dict[str, Any], runner: Runner, cap: tuple[int, ZoneInfo]
) -> dict[str, Any]:
    now = _now()
    checked = job.get("checked_at")
    recent = isinstance(checked, str) and datetime.fromisoformat(checked) + CHECK_INTERVAL > now
    if job["state"] == "waiting" and recent:
        return job  # Checked recently; no tight polling.
    with workspace_lock(Path(job["workspace"])):  # Reentrant: the adapters take it again.
        try:
            _revalidate(job)
            step, reason = _step(job)
        except (ValueError, OSError) as invalid:
            text = _message(invalid)
            if job["state"] == "waiting":  # A known container: left untouched, never recreated.
                return _update(
                    settings, job["id"], {"waiting"}, error=text, checked_at=now.isoformat()
                )
            return _update(
                settings, job["id"], RUNNABLE, state="blocked", started_at=None, error=text
            )
        if step == "none":
            found = outcome(job)
            # _step returns "none" only when a receipt exists.
            assert found is not None  # nosec B101
            state = found[0] if job.get("started_at") else "skipped"
            return _update(
                settings, job["id"], RUNNABLE, state=state, result=found[1], error=reason
            )
        # Reading a known Instagram container (finish) is a continuation, not a new clip.
        if step != "finish" and _cap_reached(settings, job, now, cap):
            note = f"Daily cap of {cap[0]} clips for this platform reached; deferred."
            return _update(settings, job["id"], RUNNABLE, error=note)
        # Atomic, durable claim before any remote side effect; a concurrent cancel wins.
        claimed = _update(
            settings,
            job["id"],
            {job["state"]},
            state="running",
            started_at=job.get("started_at") or now.isoformat(),
            checked_at=now.isoformat(),
        )
        if claimed["state"] != "running":
            return claimed
        failure: BaseException | None = None
        result: Any = None
        try:
            result = runner(_argv(job, step))
        except (Exception, SystemExit) as caught:
            failure = caught
        try:
            found = outcome(job)
        except (ValueError, OSError):
            return _update(
                settings,
                job["id"],
                {"running"},
                state="unknown",
                error="Receipts unreadable after the run; reconcile manually.",
            )
    message = _message(failure) if failure else None
    if found:
        return _update(
            settings, job["id"], {"running"}, state=found[0], result=found[1], error=message
        )
    skipped = _skip_reason(result) if failure is None else None
    if skipped:
        return _update(settings, job["id"], {"running"}, state="skipped", error=skipped)
    hint = SETUP[job["destination"]]
    detail = message or "The adapter reported nothing to publish."
    note = f"{detail} Nothing was sent. {hint}"
    return _update(settings, job["id"], {"running"}, state="blocked", started_at=None, error=note)


def run(
    settings: Settings,
    runner: Runner,
    *,
    execute: bool,
    max_per_day: int = MAX_PER_DAY,
    timezone: str = "UTC",
) -> dict[str, Any]:
    """Preview due jobs offline, or execute them through ``runner`` (the CLI boundary)."""
    now = _now()
    cap = (max_per_day, check_cap(max_per_day, timezone))
    if not execute:
        with jobs_lock(settings) as path:
            jobs = load(path)
        due = []
        for job in jobs:
            if job["state"] not in RUNNABLE or datetime.fromisoformat(job["at_utc"]) > now:
                continue
            item = {k: job[k] for k in ("id", "file", "destination", "identity", "state", "at")}
            try:
                with workspace_lock(Path(job["workspace"])):
                    _revalidate(job)
                    item["step"], item["reason"] = _step(job)
                if item["step"] not in {"finish", "none"} and _cap_reached(settings, job, now, cap):
                    item.update(step="deferred", reason="daily cap reached")
            except (ValueError, OSError) as error:
                item.update(step="blocked", reason=_message(error))
            due.append(item)
        limits = {"max_per_platform_day": max_per_day, "timezone": timezone}
        return {"mode": "preview", "now": now.isoformat(), **limits, "due": due, "remote_calls": 0}
    with scheduler_lock(settings):
        with jobs_lock(settings) as path:
            jobs = load(path)
        # Read receipts without holding jobs_lock: enqueue takes workspace then jobs locks.
        # Existing adapters persist intent before writes, so a known processing container
        # can continue through finish; publishing/unknown receipts can never be resent.
        for job in jobs:
            if job["state"] != "running":
                continue
            try:
                with workspace_lock(Path(job["workspace"])):
                    recovered = outcome(job)
            except (ValueError, OSError):
                recovered = None
            state, result = recovered or ("interrupted", None)
            _update(settings, job["id"], {"running"}, state=state, result=result)
        with jobs_lock(settings) as path:
            jobs = load(path)
        due = sorted(
            (
                j
                for j in jobs
                if j["state"] in RUNNABLE and datetime.fromisoformat(j["at_utc"]) <= now
            ),
            key=lambda j: j["at_utc"],
        )
        done = []
        for snapshot in due:
            with jobs_lock(settings) as path:
                current = load(path)
            job = next(j for j in current if j["id"] == snapshot["id"])
            if job["state"] not in RUNNABLE:  # Cancelled meanwhile.
                continue
            others = [j for j in current if j["id"] != job["id"] and _key(j) == _key(job)]
            busy = [j["id"] for j in others if j["state"] in OCCUPIED]
            if busy:
                done.append(
                    _update(
                        settings,
                        job["id"],
                        RUNNABLE,
                        state="skipped",
                        error=f"Job {busy[0]} already covers this media and account.",
                    )
                )
                continue
            done.append(_run_one(settings, job, runner, cap))
    return {"mode": "execute", "now": now.isoformat(), "jobs": done}
