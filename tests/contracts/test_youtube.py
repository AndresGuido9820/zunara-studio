"""YouTube adapter contract, exercised against a fake SDK. Nothing here reaches the network."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from zunara import media, queue, youtube

pytestmark = pytest.mark.contract

CHANNEL = "UCabcdefghijklmnopqrstuv"
PROBE_OK = {"duration_s": 12.0, "width": 1080, "height": 1920, "codec": "h264", "has_audio": True}


class FakeRequest:
    def __init__(self, steps: list[Any]) -> None:
        self.steps = list(steps)

    def next_chunk(self) -> tuple[Any, Any]:
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class FakeYouTube:
    """Minimal stand-in for ``googleapiclient`` ``youtube`` v3 resources."""

    def __init__(self, work: Path, channel_id: str = CHANNEL) -> None:
        self.work = work
        self.channel_items = [{"id": channel_id, "snippet": {"title": "Zunara Test"}}]
        self.channel_calls: list[dict[str, Any]] = []
        self.inserts: list[dict[str, Any]] = []
        self.queue_at_insert: list[list[dict[str, Any]]] = []
        self.steps: list[list[Any]] = []

    def channels(self) -> "FakeYouTube":
        return self

    def videos(self) -> "FakeYouTube":
        return self

    def list(self, **kwargs: Any) -> "FakeYouTube":
        self.channel_calls.append(kwargs)
        return self

    def execute(self) -> dict[str, Any]:
        return {"items": self.channel_items}

    def insert(self, **kwargs: Any) -> FakeRequest:
        self.queue_at_insert.append(read_queue(self.work))
        self.inserts.append(kwargs)
        number = len(self.inserts)
        steps = self.steps.pop(0) if self.steps else [(0.5, None), (None, {"id": f"vid{number}"})]
        return FakeRequest(steps)


class RecordingMedia:
    """``media_factory`` that keeps the bytes it was given; the path may be a temporary copy."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, filename: str, **kwargs: Any) -> dict[str, Any]:
        path = Path(filename)
        self.calls.append({"name": path.name, "bytes": path.read_bytes(), **kwargs})
        return {"media": path.suffix}


def read_queue(work: Path) -> list[dict[str, Any]]:
    path = work / "out" / "queue.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def make_workspace(tmp_path: Path, clips: dict[str, bytes], *, approve: bool = True) -> Path:
    work = tmp_path / "session-01"
    (work / "out").mkdir(parents=True)
    for stem, content in clips.items():
        (work / "out" / f"{stem}.mp4").write_bytes(content)
    rows = queue.refresh(work)
    for row in rows:
        stem = Path(row["file"]).stem
        row.update(title=f"Clip {stem}", description=f"About {stem}.", tags=["futbol", stem])
    queue.save(work, rows)
    if approve:
        queue.approve(work, set(clips))
    return work


def future(hours: float = 48) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


@pytest.fixture
def probed(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    paths: list[Path] = []

    def fake_probe(path: Path) -> dict[str, Any]:
        paths.append(Path(path))
        return dict(PROBE_OK)

    monkeypatch.setattr(youtube, "probe", fake_probe, raising=False)
    monkeypatch.setattr(media, "probe", fake_probe)
    return paths


@pytest.fixture
def work(tmp_path: Path, probed: list[Path]) -> Path:
    return make_workspace(tmp_path, {"a": b"first clip bytes", "b": b"second clip bytes"})


# Previews


def test_preview_plans_without_remote_writes(work: Path) -> None:
    api = FakeYouTube(work)
    media_factory = RecordingMedia()
    before = (work / "out" / "queue.json").read_bytes()

    plan = youtube.upload(work, api, channel_id=CHANNEL, media_factory=media_factory)

    assert plan == [
        {"file": "a.mp4", "channel_id": CHANNEL, "privacy": "private", "scheduled_at": None},
        {"file": "b.mp4", "channel_id": CHANNEL, "privacy": "private", "scheduled_at": None},
    ]
    assert api.inserts == []
    assert media_factory.calls == []
    assert (work / "out" / "queue.json").read_bytes() == before


def test_preview_checks_identity_with_readonly_call(work: Path) -> None:
    api = FakeYouTube(work)

    youtube.upload(work, api, channel_id=CHANNEL)

    assert api.channel_calls == [{"part": "snippet", "mine": True}]


@pytest.mark.parametrize("privacy", ["unlisted", "public"])
def test_preview_keeps_requested_privacy_without_schedule(work: Path, privacy: str) -> None:
    plan = youtube.upload(work, FakeYouTube(work), channel_id=CHANNEL, privacy=privacy)

    assert {p["privacy"] for p in plan} == {privacy}


def test_preview_spaces_scheduled_clips_and_forces_private(work: Path) -> None:
    start = future()

    plan = youtube.upload(
        work,
        FakeYouTube(work),
        channel_id=CHANNEL,
        privacy="public",
        schedule=start,
        every_hours=2.5,
    )

    first = datetime.fromisoformat(start)
    assert [datetime.fromisoformat(p["scheduled_at"]) for p in plan] == [
        first,
        first + timedelta(hours=2.5),
    ]
    assert {p["privacy"] for p in plan} == {"private"}


def test_preview_honours_selection_and_skips_published(work: Path) -> None:
    rows = queue.load(work)
    rows[0]["youtube_id"] = "already"
    queue.save(work, rows)

    assert youtube.upload(work, FakeYouTube(work), channel_id=CHANNEL) == [
        {"file": "b.mp4", "channel_id": CHANNEL, "privacy": "private", "scheduled_at": None}
    ]
    assert youtube.upload(work, FakeYouTube(work), channel_id=CHANNEL, only={"a"}) == []


# Channel identity


@pytest.mark.parametrize(
    "channel_id",
    ["", "UCother", CHANNEL.lower(), CHANNEL[:-1], CHANNEL + "x", f" {CHANNEL}", "Zunara Test"],
)
def test_channel_must_match_exactly(work: Path, channel_id: str) -> None:
    api = FakeYouTube(work)
    before = read_queue(work)

    with pytest.raises(ValueError, match="does not match"):
        youtube.upload(
            work, api, channel_id=channel_id, execute=True, media_factory=RecordingMedia()
        )

    assert api.inserts == []
    assert read_queue(work) == before


def test_account_without_channel_is_rejected(work: Path) -> None:
    api = FakeYouTube(work)
    api.channel_items = []

    with pytest.raises(ValueError, match="no YouTube channel"):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())
    assert api.inserts == []


def test_identity_returns_first_channel() -> None:
    api = FakeYouTube(Path("."), channel_id="UCxyz")

    assert youtube.identity(api) == {"id": "UCxyz", "title": "Zunara Test"}


# Schedule and option validation


@pytest.mark.parametrize(
    ("schedule", "every_hours", "message"),
    [
        ("2030-01-01T12:00:00", 3, "timezone"),
        ("2001-01-01T12:00:00+00:00", 3, "future"),
        (None, 0, "every-hours"),
        (None, -1, "every-hours"),
        (None, float("nan"), "every-hours"),
        (None, float("inf"), "every-hours"),
    ],
)
def test_invalid_schedule_fails_before_remote_writes(
    work: Path, schedule: str | None, every_hours: float, message: str
) -> None:
    api = FakeYouTube(work)
    before = read_queue(work)

    with pytest.raises(ValueError, match=message):
        youtube.upload(
            work,
            api,
            channel_id=CHANNEL,
            schedule=schedule,
            every_hours=every_hours,
            execute=True,
            media_factory=RecordingMedia(),
        )

    assert api.inserts == []
    assert read_queue(work) == before


@pytest.mark.parametrize("schedule", ["tomorrow", "2030-13-01T00:00:00+00:00", ""])
def test_unparseable_schedule_is_rejected(schedule: str) -> None:
    with pytest.raises(ValueError):
        youtube.schedule_start(schedule, 3)


def test_schedule_keeps_offset() -> None:
    start = (datetime.now(UTC) + timedelta(days=2)).astimezone().replace(microsecond=0)

    assert youtube.schedule_start(start.isoformat(), 3) == start


def test_unknown_privacy_is_rejected(work: Path) -> None:
    api = FakeYouTube(work)

    with pytest.raises(ValueError, match="privacy"):
        youtube.upload(work, api, channel_id=CHANNEL, privacy="friends", execute=True)
    assert api.inserts == []


# Preflight


def test_unknown_selector_fails_before_remote_writes(work: Path) -> None:
    api = FakeYouTube(work)

    with pytest.raises(ValueError, match="Unknown clip names"):
        youtube.upload(work, api, channel_id=CHANNEL, only={"a", "zz"}, execute=True)
    assert api.inserts == []


def test_unapproved_clip_blocks_whole_batch(tmp_path: Path, probed: list[Path]) -> None:
    work = make_workspace(tmp_path, {"a": b"a", "b": b"b"}, approve=False)
    queue.approve(work, {"a"})
    api = FakeYouTube(work)

    with pytest.raises(ValueError, match="approval"):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())
    assert api.inserts == []
    assert all(r.get("upload_state") == "pending" for r in read_queue(work))


def test_media_changed_after_approval_blocks_whole_batch(work: Path) -> None:
    (work / "out" / "b.mp4").write_bytes(b"re-rendered")
    api = FakeYouTube(work)

    with pytest.raises(ValueError, match="changed after approval"):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())
    assert api.inserts == []


@pytest.mark.parametrize(
    "info", [{"has_audio": False, "duration_s": 12.0}, {"has_audio": True, "duration_s": 0.0}]
)
def test_clip_without_audio_or_duration_is_rejected(
    work: Path, monkeypatch: pytest.MonkeyPatch, info: dict[str, Any]
) -> None:
    def fake_probe(path: Path) -> dict[str, Any]:
        return {**PROBE_OK, **info}

    monkeypatch.setattr(youtube, "probe", fake_probe, raising=False)
    monkeypatch.setattr(media, "probe", fake_probe)
    api = FakeYouTube(work)

    with pytest.raises(ValueError, match="video and audio"):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())
    assert api.inserts == []


# Successful uploads


def test_execute_uploads_approved_bytes_with_expected_request(work: Path) -> None:
    api = FakeYouTube(work)
    media_factory = RecordingMedia()

    plan = youtube.upload(
        work, api, channel_id=CHANNEL, privacy="unlisted", execute=True, media_factory=media_factory
    )

    assert [p["file"] for p in plan] == ["a.mp4", "b.mp4"]
    assert [c["bytes"] for c in media_factory.calls] == [b"first clip bytes", b"second clip bytes"]
    for call in media_factory.calls:
        assert call["name"].endswith(".mp4")
        assert call["resumable"] is True
        assert call["chunksize"] > 0

    first = api.inserts[0]
    assert set(first["part"].split(",")) == {"snippet", "status"}
    assert first["body"]["snippet"]["title"] == "Clip a"
    assert first["body"]["snippet"]["description"] == "About a."
    assert first["body"]["snippet"]["tags"] == ["futbol", "a"]
    assert first["body"]["status"]["privacyStatus"] == "unlisted"
    assert first["body"]["status"]["selfDeclaredMadeForKids"] is False
    assert "publishAt" not in first["body"]["status"]
    assert first["media_body"] == {"media": ".mp4"}


def test_execute_records_video_ids(work: Path) -> None:
    youtube.upload(
        work, FakeYouTube(work), channel_id=CHANNEL, execute=True, media_factory=RecordingMedia()
    )

    rows = {r["file"]: r for r in read_queue(work)}
    assert rows["a.mp4"]["youtube_id"] == "vid1"
    assert rows["b.mp4"]["youtube_id"] == "vid2"
    for row in rows.values():
        assert row["upload_state"] == "uploaded"
        assert row["channel_id"] == CHANNEL
        assert datetime.fromisoformat(row["uploaded_at"]).tzinfo is not None


def test_execute_follows_resumable_chunks_until_response(work: Path) -> None:
    api = FakeYouTube(work)
    api.steps = [[(0.1, None), (0.4, None), (0.9, None), (None, {"id": "long"})]]

    youtube.upload(
        work, api, channel_id=CHANNEL, only={"a"}, execute=True, media_factory=RecordingMedia()
    )

    assert read_queue(work)[0]["youtube_id"] == "long"


def test_scheduled_execute_sends_publish_at(work: Path) -> None:
    api = FakeYouTube(work)
    start = future()

    youtube.upload(
        work,
        api,
        channel_id=CHANNEL,
        privacy="public",
        schedule=start,
        every_hours=1,
        execute=True,
        media_factory=RecordingMedia(),
    )

    statuses = [i["body"]["status"] for i in api.inserts]
    assert {s["privacyStatus"] for s in statuses} == {"private"}
    published = [datetime.fromisoformat(s["publishAt"]) for s in statuses]
    assert published == [
        datetime.fromisoformat(start),
        datetime.fromisoformat(start) + timedelta(hours=1),
    ]
    rows = read_queue(work)
    assert [datetime.fromisoformat(r["scheduled_at"]) for r in rows] == published


def test_second_run_does_not_upload_again(work: Path) -> None:
    youtube.upload(
        work, FakeYouTube(work), channel_id=CHANNEL, execute=True, media_factory=RecordingMedia()
    )
    api = FakeYouTube(work)

    assert youtube.upload(work, api, channel_id=CHANNEL, execute=True) == []
    assert api.inserts == []


# Ambiguous outcomes


def test_intent_is_persisted_before_each_remote_call(work: Path) -> None:
    api = FakeYouTube(work)

    youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())

    first, second = ({r["file"]: r for r in rows} for rows in api.queue_at_insert)
    assert first["a.mp4"]["upload_state"] == "uploading"
    assert first["a.mp4"]["channel_id"] == CHANNEL
    assert first["a.mp4"].get("youtube_id") is None
    assert first["b.mp4"]["upload_state"] == "pending"
    assert second["a.mp4"]["youtube_id"] == "vid1"
    assert second["b.mp4"]["upload_state"] == "uploading"


@pytest.mark.parametrize(
    "failure", [TimeoutError("socket"), ConnectionResetError(), KeyboardInterrupt()]
)
def test_interrupted_upload_stays_ambiguous(work: Path, failure: BaseException) -> None:
    api = FakeYouTube(work)
    api.steps = [[(0.3, None), failure]]

    with pytest.raises(type(failure)):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())

    rows = {r["file"]: r for r in read_queue(work)}
    assert rows["a.mp4"]["upload_state"] == "uploading"
    assert rows["a.mp4"].get("youtube_id") is None
    assert rows["b.mp4"]["upload_state"] == "pending"
    assert len(api.inserts) == 1


def test_insert_failure_stays_ambiguous(work: Path) -> None:
    class RejectingYouTube(FakeYouTube):
        def insert(self, **kwargs: Any) -> FakeRequest:
            super().insert(**kwargs)
            raise OSError("connection dropped")

    api = RejectingYouTube(work)

    with pytest.raises(OSError):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())
    assert read_queue(work)[0]["upload_state"] == "uploading"


def test_ambiguous_upload_is_never_retried(work: Path) -> None:
    api = FakeYouTube(work)
    api.steps = [[TimeoutError("socket")]]
    with pytest.raises(TimeoutError):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())

    retry = FakeYouTube(work)
    with pytest.raises(ValueError, match="unknown outcome"):
        youtube.upload(
            work, retry, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia()
        )
    assert retry.inserts == []
    with pytest.raises(ValueError, match="unknown outcome"):
        youtube.upload(work, FakeYouTube(work), channel_id=CHANNEL)


@pytest.mark.parametrize("state", ["failed", "UPLOADING", "", None, 3])
def test_unknown_upload_state_fails_before_remote_writes(work: Path, state: object) -> None:
    rows = queue.load(work)
    rows[0]["upload_state"] = state
    queue.save(work, rows)
    api = FakeYouTube(work)

    with pytest.raises(ValueError):
        youtube.upload(work, api, channel_id=CHANNEL, execute=True, media_factory=RecordingMedia())
    assert api.inserts == []


def test_media_swapped_after_verification_never_uploads_other_bytes(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def swapping_probe(path: Path) -> dict[str, Any]:
        (work / "out" / "a.mp4").write_bytes(b"swapped after approval")
        return dict(PROBE_OK)

    monkeypatch.setattr(youtube, "probe", swapping_probe, raising=False)
    monkeypatch.setattr(media, "probe", swapping_probe)
    api = FakeYouTube(work)
    media_factory = RecordingMedia()

    try:
        youtube.upload(
            work, api, channel_id=CHANNEL, only={"a"}, execute=True, media_factory=media_factory
        )
    except ValueError:
        assert api.inserts == []
        assert read_queue(work)[0]["upload_state"] == "pending"
    else:
        assert [c["bytes"] for c in media_factory.calls] == [b"first clip bytes"]


def test_upload_reads_snapshot_not_live_workspace_file(work: Path) -> None:
    original = work / "out" / "a.mp4"

    class EditingMedia(RecordingMedia):
        def __call__(self, filename: str, **kwargs: Any) -> dict[str, Any]:
            original.write_bytes(b"edited while uploading")
            return super().__call__(filename, **kwargs)

    media_factory = EditingMedia()
    youtube.upload(
        work,
        FakeYouTube(work),
        channel_id=CHANNEL,
        only={"a"},
        execute=True,
        media_factory=media_factory,
    )

    assert [c["bytes"] for c in media_factory.calls] == [b"first clip bytes"]
