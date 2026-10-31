"""Local jobs: offline preview, durable intent, receipt-driven outcomes and no automatic retries."""

import hashlib
import json
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from filelock import FileLock

from zunara import browser, cli, instagram, queue, scheduler, social, youtube
from zunara.config import Settings

CHANNEL = "UC" + "a" * 22
ACCOUNT = "17841400000000001"
T0 = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
AT = "2030-01-01T08:00:00-05:00"  # 13:00 UTC
YT = {"channel": "main", "channel_id": CHANNEL}
IG = {"account_id": ACCOUNT, "username": "@Studio.Clips"}


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ZUNARA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(instagram.TOKEN_ENV, raising=False)
    clock(monkeypatch, T0)
    return Settings.from_env()


def clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(scheduler, "_now", lambda: moment)


def workspace(root: Path, clips: tuple[str, ...] = ("clip-01",)) -> Path:
    (root / "out").mkdir(parents=True)
    rows = []
    for stem in clips:
        content = stem.encode() * 8
        (root / "out" / f"{stem}.mp4").write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        rows.append(
            {"file": f"{stem}.mp4", "title": "T", "description": "D #tag", "tags": ["a"]}
            | {"topic": "music", "sha256": sha, "approved_sha256": sha}
        )
    (root / "out" / "queue.json").write_text(json.dumps(rows))
    return root


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return workspace(tmp_path / "session-01", ("clip-01", "clip-02", "clip-03"))


class Runner:
    """Fake CLI boundary: records argv and lets a test simulate the adapter's receipts."""

    def __init__(self, effect: Callable[[list[str]], Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.effect = effect

    def __call__(self, argv: list[str]) -> Any:
        self.calls.append(argv)
        return self.effect(argv) if self.effect else {}


class FakeYouTube:
    """Just enough of the YouTube v3 client: identity read and a one-chunk insert."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def channels(self) -> "FakeYouTube":
        return self

    def videos(self) -> "FakeYouTube":
        return self

    def list(self, **kwargs: Any) -> "FakeYouTube":
        return self

    def execute(self) -> dict[str, Any]:
        return {"items": [{"id": CHANNEL, "snippet": {"title": "Test"}}]}

    def insert(self, body: dict[str, Any], **kwargs: Any) -> "FakeYouTube":
        self.bodies.append(body)
        return self

    def next_chunk(self) -> tuple[None, dict[str, str]]:
        return None, {"id": "vid-1"}


def refuse(argv: list[str]) -> Any:
    raise AssertionError(f"No adapter may run: {argv}")


def run(settings: Settings, runner: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return list(scheduler.run(settings, runner, execute=True, **kwargs)["jobs"])


def set_row(work: Path, stem: str, **fields: Any) -> None:
    rows = queue.load(work)
    next(r for r in rows if r["file"] == f"{stem}.mp4").update(fields)
    queue.save(work, rows)


def native_receipt(work: Path, stem: str, state: str, **extra: Any) -> None:
    ledger = instagram.load_ledger(work)
    row = next(r for r in queue.load(work) if r["file"] == f"{stem}.mp4")
    key = (ACCOUNT, row["approved_sha256"])
    ledger["records"] = [
        r for r in ledger["records"] if (r["account_id"], r["approved_sha256"]) != key
    ]
    record = {"account_id": ACCOUNT, "username": "studio.clips", "file": row["file"]}
    record.update(approved_sha256=row["approved_sha256"], api_version="v26.0", fields={})
    ledger["records"].append({**record, "state": state, "container_id": "901", **extra})
    instagram._save(work, ledger)


def test_add_freezes_metadata_and_identity_in_a_private_versioned_file(
    settings: Settings, work: Path
) -> None:
    job = scheduler.add(settings, work, "clip-01", "instagram", AT, {**IG, "ai_generated": True})
    assert job["state"] == "pending" and job["at_utc"] == "2030-01-01T13:00:00+00:00"
    assert job["identity"] == {"account_id": ACCOUNT, "username": "studio.clips"}
    assert job["frozen"]["title"] == "T" and job["frozen"]["ai_generated"] is True
    path = settings.data_dir / "scheduler" / "jobs.json"
    assert json.loads(path.read_text())["version"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("destination", "at", "options", "message"),
    [
        ("tiktok", AT, {}, "Native TikTok publishing is not implemented"),
        ("youtube", "2030-01-01T08:00:00", YT, "explicit UTC offset"),
        ("youtube", "2029-12-31T08:00:00+00:00", YT, "in the future"),
        ("youtube", AT, {"channel": "main", "channel_id": "UCshort"}, "exact UC"),
        ("instagram", AT, {"username": "x"}, "--account-id"),
        ("instagram-browser", AT, {"username": "x", "privacy": "public"}, "only to youtube"),
    ],
)
def test_add_rejects_invalid_or_unsupported_jobs(
    settings: Settings, work: Path, destination: str, at: str, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        scheduler.add(settings, work, "clip-01", destination, at, options)


def test_add_requires_approval_and_dedups_across_workspaces_and_instagram_routes(
    settings: Settings, work: Path, tmp_path: Path
) -> None:
    set_row(work, "clip-03", approved_sha256=None)
    with pytest.raises(ValueError, match="approval"):
        scheduler.add(settings, work, "clip-03", "youtube", AT, YT)
    scheduler.add(settings, work, "clip-01", "instagram", AT, IG)
    copy = workspace(tmp_path / "copy")  # Same bytes in another workspace.
    browser_route = {"username": "STUDIO.CLIPS", "connection": "chrome"}
    with pytest.raises(ValueError, match="already exists"):
        scheduler.add(settings, copy, "clip-01", "instagram-browser", AT, browser_route)
    scheduler.add(settings, copy, "clip-01", "youtube", AT, YT)  # Another platform is fine.


def test_preview_through_the_cli_makes_no_remote_calls(
    settings: Settings,
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    scheduler.add(settings, work, "clip-02", "instagram", AT, IG)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def no_remote(*args: object, **kwargs: object) -> None:
        raise AssertionError("Preview must not open a client.")

    monkeypatch.setattr(youtube, "client", no_remote)
    monkeypatch.setattr(instagram, "Client", no_remote)
    monkeypatch.setattr("requests.Session.request", no_remote)
    cli.main(["jobs", "run"])
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "preview" and result["remote_calls"] == 0
    assert [i["step"] for i in result["due"]] == ["upload", "publish"]
    assert {j["state"] for j in scheduler.listing(settings)["jobs"]} == {"pending"}


def test_jobs_are_not_run_before_they_are_due(settings: Settings, work: Path) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    assert run(settings, Runner(refuse)) == []


def test_youtube_job_runs_through_the_real_cli_once_and_keeps_privacy_honest(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    clock(monkeypatch, T0 + timedelta(hours=2))
    api = FakeYouTube()
    opened: list[str] = []
    monkeypatch.setattr(youtube, "client", lambda s, channel: opened.append(channel) or api)
    monkeypatch.setattr(youtube, "probe", lambda p: {"has_audio": True, "duration_s": 9.0})
    monkeypatch.setattr("googleapiclient.http.MediaFileUpload", lambda *a, **k: object())
    runner = lambda argv: cli.run(cli.parser().parse_args(argv))  # noqa: E731
    [job] = run(settings, runner)
    assert job["state"] == "succeeded" and opened == ["main"]
    assert job["result"]["video_id"] == queue.load(work)[0]["youtube_id"]
    assert job["result"]["privacy_requested"] == "private"
    assert "not verified" in job["result"]["visibility"]
    assert api.bodies[0]["status"]["privacyStatus"] == "private"
    assert run(settings, Runner(refuse)) == []


def test_missing_setup_blocks_with_hints_and_is_retried_only_because_nothing_was_sent(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def unauthorized(argv: list[str]) -> Any:
        raise ValueError("Authorize this channel first: zunara auth --channel main")

    [job] = run(settings, Runner(unauthorized))
    assert job["state"] == "blocked" and job["started_at"] is None
    assert "Nothing was sent" in job["error"] and "zunara auth" in job["error"]
    assert "youtube" in scheduler.listing(settings)["setup"]
    again = Runner()
    run(settings, again)
    assert len(again.calls) == 1


def test_started_upload_becomes_unknown_and_is_never_retried(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def dropped(argv: list[str]) -> Any:
        set_row(work, "clip-01", upload_state="uploading")
        raise RuntimeError("socket closed; access_token=SECRET-VALUE")

    [job] = run(settings, Runner(dropped))
    assert job["state"] == "unknown" and "SECRET" not in json.dumps(job)
    assert run(settings, Runner(refuse)) == []


def test_a_job_left_running_is_marked_interrupted_and_never_retried(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def crash(argv: list[str]) -> Any:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(settings, Runner(crash))
    assert scheduler.listing(settings)["jobs"][0]["state"] == "running"
    assert run(settings, Runner(refuse)) == []
    assert scheduler.listing(settings)["jobs"][0]["state"] == "interrupted"
    with pytest.raises(ValueError, match="only pending or blocked"):
        scheduler.cancel(settings, scheduler.listing(settings)["jobs"][0]["id"])


def test_native_instagram_resumes_the_known_container_and_never_recreates_it(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "instagram", AT, IG)

    def adapter(argv: list[str]) -> Any:
        if argv[1] == "publish":
            native_receipt(work, "clip-01", "processing")
        elif runner.calls.count(argv) == 2:  # Second finish: Meta reports FINISHED.
            native_receipt(work, "clip-01", "published", media_id="777")
        return {"items": []}

    runner = Runner(adapter)
    clock(monkeypatch, T0 + timedelta(hours=2))
    [job] = run(settings, runner)
    assert job["state"] == "waiting" and job["result"]["container_id"] == "901"
    run(settings, runner)  # Within a minute: no tight polling.
    for minutes in (2, 4):
        clock(monkeypatch, T0 + timedelta(hours=2, minutes=minutes))
        [job] = run(settings, runner)
    assert [a[1] for a in runner.calls] == ["publish", "finish", "finish"]
    assert "--ai-generated" not in runner.calls[0] and "--execute" in runner.calls[1]
    assert job["state"] == "succeeded" and job["result"]["media_id"] == "777"


@pytest.mark.parametrize(("receipt", "expected"), [("unknown", "unknown"), ("failed", "failed")])
def test_ambiguous_or_rejected_instagram_creation_is_final(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch, receipt: str, expected: str
) -> None:
    scheduler.add(settings, work, "clip-01", "instagram", AT, IG)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def create(argv: list[str]) -> Any:
        native_receipt(work, "clip-01", receipt, container_id=None)
        raise ValueError("Container creation outcome unknown.")

    [job] = run(settings, Runner(create))
    assert job["state"] == expected
    assert run(settings, Runner(refuse)) == []


def test_changed_metadata_blocks_before_any_adapter_runs(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    set_row(work, "clip-01", title="Edited after scheduling")
    clock(monkeypatch, T0 + timedelta(hours=2))
    [job] = run(settings, Runner(refuse))
    assert job["state"] == "blocked" and "title changed" in job["error"]


def test_browser_job_uses_the_selected_connection_and_records_the_permalink(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = {"username": "studio.clips", "connection": "chrome"}
    scheduler.add(settings, work, "clip-01", "instagram-browser", AT, options)
    sha = queue.load(work)[0]["approved_sha256"]

    def share(argv: list[str]) -> Any:
        record = {"platform": "instagram", "username": "studio.clips", "approved_sha256": sha}
        record.update(file="clip-01.mp4", state="published", status=None, connection="chrome")
        record.update(media_selected=True, share_clicked=True, permalink="https://x/reel/1/")
        browser._save(work, {"version": 1, "records": [record]})

    runner = Runner(share)
    clock(monkeypatch, T0 + timedelta(hours=2))
    [job] = run(settings, runner)
    assert runner.calls[0][-3:] == ["--connection", "chrome", "--execute"]
    assert job["state"] == "succeeded" and job["result"]["permalink"] == "https://x/reel/1/"


def test_daily_cap_counts_started_writes_per_platform_in_the_given_timezone(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for stem in ("clip-01", "clip-02", "clip-03"):
        scheduler.add(settings, work, stem, "youtube", AT, YT)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def upload(argv: list[str]) -> Any:
        set_row(
            work, argv[3], youtube_id=f"id-{argv[3]}", upload_state="uploaded", channel_id=CHANNEL
        )

    runner = Runner(upload)
    jobs = run(settings, runner, max_per_day=2, timezone="America/Bogota")
    assert [j["state"] for j in jobs] == ["succeeded", "succeeded", "pending"]
    assert "Daily cap" in jobs[2]["error"] and len(runner.calls) == 2
    clock(monkeypatch, datetime(2030, 1, 2, 4, 0, tzinfo=UTC))  # Still Jan 1 in Bogotá.
    assert run(settings, runner, timezone="America/Bogota")[0]["state"] == "pending"
    clock(monkeypatch, datetime(2030, 1, 2, 5, 30, tzinfo=UTC))  # Jan 2 in Bogotá.
    assert run(settings, runner, timezone="America/Bogota")[0]["state"] == "succeeded"
    with pytest.raises(ValueError, match="IANA"):
        scheduler.run(settings, runner, execute=False, timezone="Mars/Base")


def test_cancel_and_the_global_scheduler_lock(settings: Settings, work: Path) -> None:
    job = scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    assert scheduler.cancel(settings, job["id"])["state"] == "cancelled"
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)  # Cancelled: may be re-added.
    lock = FileLock(str(settings.data_dir / "scheduler" / ".scheduler.lock"))
    with lock, pytest.raises(ValueError, match="Another"):
        run(settings, Runner(refuse))


def test_a_job_cancelled_after_the_run_started_is_not_claimed(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    second = scheduler.add(settings, work, "clip-02", "youtube", AT, YT)
    clock(monkeypatch, T0 + timedelta(hours=2))

    def cancel_next(argv: list[str]) -> Any:
        scheduler.cancel(settings, second["id"])  # Arrives while the first job runs.
        set_row(work, "clip-01", youtube_id="v1", upload_state="uploaded", channel_id=CHANNEL)

    runner = Runner(cancel_next)
    jobs = run(settings, runner)
    assert [j["id"] for j in jobs] == [first["id"]] and len(runner.calls) == 1
    states = {j["id"]: j["state"] for j in scheduler.listing(settings)["jobs"]}
    assert states == {first["id"]: "succeeded", second["id"]: "cancelled"}


def test_an_upload_to_another_channel_is_not_reported_as_success(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    set_row(work, "clip-01", youtube_id="v1", upload_state="uploaded", channel_id="UC" + "b" * 22)
    clock(monkeypatch, T0 + timedelta(hours=2))
    [job] = run(settings, Runner(refuse))
    assert job["state"] == "skipped" and "another" in job["result"]["reason"]


@pytest.mark.parametrize("destination", ["instagram-browser", "youtube"])
def test_ai_disclosure_is_refused_where_it_would_be_dropped(
    settings: Settings, work: Path, destination: str
) -> None:
    options = {**YT, "username": "studio.clips", "ai_generated": True}
    with pytest.raises(ValueError, match="only for native instagram"):
        scheduler.add(settings, work, "clip-01", destination, AT, options)


def test_a_waiting_container_survives_revalidation_errors_without_writes(
    settings: Settings, work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler.add(settings, work, "clip-01", "instagram", AT, IG)
    clock(monkeypatch, T0 + timedelta(hours=2))
    run(settings, Runner(lambda argv: native_receipt(work, "clip-01", "processing")))
    path = settings.data_dir / "scheduler" / "jobs.json"
    before = path.read_bytes()
    set_row(work, "clip-01", description="edited")
    clock(monkeypatch, T0 + timedelta(hours=3))
    [job] = run(settings, Runner(refuse))
    assert job["state"] == "waiting" and "description changed" in job["error"]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("manual", "provider", "state", "reason"),
    [
        ({"status": "published"}, None, "skipped", "publications.json records published"),
        ({"status": "pending"}, None, "blocked", "unresolved youtube record"),
        (None, "accepted", "skipped", "social-publications.json records accepted"),
        (None, "unknown", "skipped", "social-publications.json records unknown"),
        (None, "completed", "skipped", "social-publications.json records completed"),
    ],
)
def test_youtube_jobs_honor_manual_and_provider_receipts_before_uploading(
    settings: Settings,
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    manual: dict[str, str] | None,
    provider: str | None,
    state: str,
    reason: str,
) -> None:
    scheduler.add(settings, work, "clip-01", "youtube", AT, YT)
    sha = queue.load(work)[0]["approved_sha256"]
    if manual:
        entry = {"platform": "youtube", "file": "clip-01.mp4", **manual}
        (work / "publications.json").write_text(json.dumps([entry]))
    if provider:
        record = {"platform": "youtube", "state": provider, "profile": "p", "approved_sha256": sha}
        social._save_ledger(work, {"version": 1, "records": [record]})
    before = {p.name: p.read_bytes() for p in (work, work / "out") for p in p.glob("*.json")}
    clock(monkeypatch, T0 + timedelta(hours=2))
    preview = scheduler.run(settings, refuse, execute=False)["due"][0]
    assert preview["step"] == ("none" if state == "skipped" else "blocked")
    [job] = run(settings, Runner(refuse))
    assert job["state"] == state and reason in json.dumps(job)
    assert before == {
        p.name: p.read_bytes() for p in (work, work / "out") for p in p.glob("*.json")
    }
