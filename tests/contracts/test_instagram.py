"""Native Instagram adapter contract against a fake HTTP session. Nothing reaches the network."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import requests

from zunara import instagram, queue

pytestmark = pytest.mark.contract

TOKEN = "EAAsynthetic-token-0123456789"  # gitleaks:allow -- synthetic contract-test credential
IG = "17841400000000001"
USER = "studio.clips"
V = "v26.0"
BASE = f"{instagram.GRAPH}/{V}"
CONTAINER = "18000000000000002"
MEDIA = "18000000000000003"
PROBE = {
    "duration_s": 12.0,
    "width": 1080,
    "height": 1920,
    "codec": "h264",
    "has_audio": True,
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
}


class Response:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self.body = body

    def json(self) -> Any:
        if self.body is None:
            raise ValueError("not json")
        return self.body


class Session:
    """Records every request and the ledger at each write; replies from scripted queues."""

    def __init__(self, work: Path) -> None:
        self.work = work
        self.calls: list[dict[str, Any]] = []
        self.replies: dict[tuple[str, str], list[Any]] = {
            ("GET", f"{BASE}/{IG}"): [Response(200, {"id": IG, "username": USER})],
            ("POST", f"{BASE}/{IG}/media"): [
                # A returned upload URI must be ignored in favor of the fixed host.
                Response(200, {"id": CONTAINER, "uri": "https://evil.example/upload"})
            ],
            ("POST", f"{instagram.UPLOAD}/{V}/{CONTAINER}"): [
                Response(200, {"success": True, "message": "Upload successful."})
            ],
            ("GET", f"{BASE}/{CONTAINER}"): [Response(200, {"status_code": "FINISHED"})],
            ("POST", f"{BASE}/{IG}/media_publish"): [Response(200, {"id": MEDIA})],
            ("GET", f"{BASE}/{MEDIA}"): [
                Response(200, {"id": MEDIA, "permalink": "https://www.instagram.com/reel/AbC_1/"})
            ],
        }

    def reply(self, method: str, url: str, *responses: Any) -> None:
        self.replies[(method, url)] = list(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        ledger = self.work / "out" / instagram.LEDGER
        call = {"method": method, "url": url, **kwargs}
        call["ledger"] = json.loads(ledger.read_text())["records"] if ledger.exists() else []
        if hasattr(kwargs.get("data"), "read"):
            call["body"] = kwargs["data"].read()
        self.calls.append(call)
        steps = self.replies[(method, url)]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def posts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


def approved(work: Path, name: str = "clip-01", content: bytes = b"reel-bytes", **row: Any) -> str:
    (work / "out").mkdir(parents=True, exist_ok=True)
    (work / "out" / f"{name}.mp4").write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    path = work / "out" / "queue.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    rows.append(
        {
            "file": f"{name}.mp4",
            "title": "A short title",
            "description": "The reviewed caption #clips",
            "tags": [],
            "sha256": sha,
            "approved_sha256": sha,
            "youtube_id": None,
            "upload_state": "pending",
            **row,
        }
    )
    path.write_text(json.dumps(rows))
    return sha


@pytest.fixture(autouse=True)
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    info = dict(PROBE)
    monkeypatch.setattr(instagram, "probe", lambda path: dict(info))
    return info


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "session-01"
    approved(root)
    return root


def client(session: Session) -> instagram.Client:
    return instagram.Client(TOKEN, session)  # type: ignore[arg-type]


def publish(
    work: Path, session: Session, *, execute: bool = True, ai: bool = False
) -> dict[str, Any]:
    items = instagram.plan(work, {"clip-01"}, IG, USER, ai)
    return instagram.publish(
        work, client(session), items, account_id=IG, username=USER, version=V, execute=execute
    )


def finish(work: Path, session: Session, *, execute: bool = True) -> dict[str, Any]:
    items = instagram.finish_plan(work, {"clip-01"}, IG, USER)
    return instagram.finish(
        work, client(session), items, account_id=IG, username=USER, version=V, execute=execute
    )


def records(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "out" / instagram.LEDGER).read_text())["records"]


def test_preview_reads_identity_only_and_writes_nothing(work: Path) -> None:
    session = Session(work)
    result = publish(work, session, execute=False)
    [call] = session.calls
    assert (call["method"], call["url"]) == ("GET", f"{BASE}/{IG}")
    assert call["params"] == {"fields": "id,username"}
    assert call["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert call["allow_redirects"] is False
    assert result["mode"] == "preview" and result["account"] == {"id": IG, "username": USER}
    [item] = result["items"]
    assert item["action"] == "create"
    assert item["fields"] == {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": "The reviewed caption #clips",
        "share_to_feed": True,
    }
    assert not (work / "out" / instagram.LEDGER).exists()


def test_execute_creates_records_and_streams_the_exact_snapshot(work: Path) -> None:
    session = Session(work)
    result = publish(work, session, ai=True)
    create, upload = session.posts()
    assert create["url"] == f"{BASE}/{IG}/media"
    assert create["json"]["is_ai_generated"] is True
    assert [r["state"] for r in create["ledger"]] == ["creating"]
    # Fixed host and saved container, never the returned "uri".
    assert upload["url"] == f"{instagram.UPLOAD}/{V}/{CONTAINER}"
    assert upload["headers"] == {
        "Authorization": f"OAuth {TOKEN}",
        "offset": "0",
        "file_size": str(len(b"reel-bytes")),
    }
    assert upload["body"] == b"reel-bytes"
    assert upload["allow_redirects"] is False
    [before] = upload["ledger"]
    assert (before["state"], before["container_id"]) == ("uploading", CONTAINER)
    [record] = records(work)
    assert record["state"] == "processing" and record["ai_generated"] is True
    assert record["media_id"] is None  # A processing container is not a post.
    assert "status" in result["next"][0] and "finish" in result["next"][1]
    assert oct(os.stat(work / "out" / instagram.LEDGER).st_mode & 0o777) == "0o600"
    assert not any("evil" in c["url"] for c in session.calls)


@pytest.mark.parametrize(
    "found", [{"id": "17841400000000999", "username": USER}, {"id": IG, "username": "other"}]
)
def test_account_mismatch_fails_before_any_write(work: Path, found: dict[str, str]) -> None:
    session = Session(work)
    session.reply("GET", f"{BASE}/{IG}", Response(200, found))
    with pytest.raises(ValueError, match="does not match"):
        publish(work, session)
    assert session.posts() == [] and not (work / "out" / instagram.LEDGER).exists()


def test_changed_media_fails_offline(work: Path) -> None:
    (work / "out" / "clip-01.mp4").write_bytes(b"edited")
    with pytest.raises(ValueError, match="approval"):
        instagram.plan(work, {"clip-01"}, IG, USER, False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"duration_s": 2.5}, "seconds"),
        ({"codec": "vp9"}, "H.264"),
        ({"audio_codec": "opus"}, "AAC"),
        ({"audio_sample_rate": 96000}, "48000 Hz"),
        ({"audio_sample_rate": None}, "48000 Hz"),
    ],
)
def test_incompatible_media_fails_offline(
    work: Path, fake_probe: dict[str, Any], change: dict[str, Any], message: str
) -> None:
    fake_probe.update(change)
    with pytest.raises(ValueError, match=message):
        instagram.plan(work, {"clip-01"}, IG, USER, False)


def test_caption_and_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "w"
    approved(work, description="x" * 2201)
    with pytest.raises(ValueError, match="2200"):
        instagram.plan(work, {"clip-01"}, IG, USER, False)
    approved(work, "big", content=b"0123456789")
    monkeypatch.setattr(instagram, "MAX_BYTES", 9)
    with pytest.raises(ValueError, match="local"):
        instagram.plan(work, {"big"}, IG, USER, False)


def test_whole_selection_is_preflighted_before_network(tmp_path: Path) -> None:
    work = tmp_path / "w"
    approved(work)
    approved(work, "clip-02", content=b"two", approved_sha256=None)
    with pytest.raises(ValueError, match="clip-02"):
        instagram.plan(work, {"clip-01", "clip-02"}, IG, USER, False)


@pytest.mark.parametrize("failure", [requests.Timeout("t"), Response(500), Response(200, {})])
def test_lost_create_response_is_never_repeated(work: Path, failure: Any) -> None:
    session = Session(work)
    session.reply("POST", f"{BASE}/{IG}/media", failure)
    with pytest.raises(ValueError, match="unknown|container ID"):
        publish(work, session)
    assert [r["state"] for r in records(work)] == ["unknown"]
    [item] = instagram.plan(work, {"clip-01"}, IG, USER, False)
    assert item["action"] == "skip" and "unknown" in item["reason"]


def test_definite_rejection_is_recorded_and_sanitized(work: Path) -> None:
    session = Session(work)
    body = {"error": {"code": 100, "message": f"bad {TOKEN} https://graph.facebook.com/x"}}
    session.reply("POST", f"{BASE}/{IG}/media", Response(400, body))
    with pytest.raises(ValueError) as error:
        publish(work, session)
    assert "error 100" in str(error.value)
    assert TOKEN not in str(error.value) and "graph.facebook" not in str(error.value)
    [record] = records(work)
    assert record["state"] == "failed" and record["phase"] == "create"
    assert instagram.plan(work, {"clip-01"}, IG, USER, False)[0]["action"] == "skip"


def test_lost_upload_keeps_container_and_status_resolves_it(work: Path) -> None:
    session = Session(work)
    session.reply("POST", f"{instagram.UPLOAD}/{V}/{CONTAINER}", requests.ConnectionError())
    with pytest.raises(ValueError, match="Upload outcome unknown"):
        publish(work, session)
    [record] = records(work)
    assert (record["state"], record["container_id"]) == ("uploading", CONTAINER)
    assert instagram.plan(work, {"clip-01"}, IG, USER, False)[0]["action"] == "skip"
    refreshed = Session(work)
    instagram.status(work, client(refreshed))
    assert [c["method"] for c in refreshed.calls] == ["GET"]
    assert records(work)[0]["state"] == "ready"


@pytest.mark.parametrize(
    ("code", "state"),
    [
        ("IN_PROGRESS", "processing"),
        ("FINISHED", "ready"),
        ("PUBLISHED", "api_published"),
        ("ERROR", "failed"),
        ("EXPIRED", "expired"),
    ],
)
def test_status_refresh_is_get_only(work: Path, code: str, state: str) -> None:
    publish(work, Session(work))
    session = Session(work)
    session.reply("GET", f"{BASE}/{CONTAINER}", Response(200, {"status_code": code}))
    result = instagram.status(work, client(session))
    assert all(c["method"] == "GET" for c in session.calls)
    assert result["records"][0]["state"] == state and result["refresh_errors"] == []


def test_status_without_client_reads_receipts_only(work: Path) -> None:
    publish(work, Session(work))
    assert instagram.status(work)["records"][0]["state"] == "processing"


def test_unrecognized_status_is_left_unresolved(work: Path) -> None:
    publish(work, Session(work))
    session = Session(work)
    session.reply("GET", f"{BASE}/{CONTAINER}", Response(200, {"status_code": "DONE?"}))
    result = instagram.status(work, client(session))
    assert result["records"][0]["state"] == "processing" and result["refresh_errors"]


def test_receipt_keeps_the_exact_creation_fields(work: Path) -> None:
    session = Session(work)
    publish(work, session, ai=True)
    [record] = records(work)
    assert record["fields"] == session.posts()[0]["json"]
    assert record["fields"]["is_ai_generated"] is True


def test_finish_preview_reads_live_status_and_saved_fields_only(work: Path) -> None:
    publish(work, Session(work))  # Left "processing"; no separate refresh is needed.
    before = (work / "out" / instagram.LEDGER).read_text()
    session = Session(work)
    result = finish(work, session, execute=False)
    assert [c["method"] for c in session.calls] == ["GET", "GET"]
    assert result["mode"] == "preview" and "records" not in result
    [item] = result["items"]
    assert (item["action"], item["container_status"]) == ("publish", "FINISHED")
    assert item["fields"]["caption"] == "The reviewed caption #clips"
    assert (work / "out" / instagram.LEDGER).read_text() == before


def test_caption_edited_after_creation_is_rejected_at_finish(work: Path) -> None:
    publish(work, Session(work))
    before = (work / "out" / instagram.LEDGER).read_text()
    path = work / "out" / "queue.json"
    rows = json.loads(path.read_text())
    rows[0]["description"] = "A new caption"
    path.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="caption .* changed"):
        instagram.finish_plan(work, {"clip-01"}, IG, USER)
    assert (work / "out" / instagram.LEDGER).read_text() == before


def test_finish_publishes_only_ready_containers(work: Path) -> None:
    publish(work, Session(work))
    # Still processing: nothing to publish, no POST.
    session = Session(work)
    session.reply("GET", f"{BASE}/{CONTAINER}", Response(200, {"status_code": "IN_PROGRESS"}))
    assert finish(work, session)["records"][0]["state"] == "processing"
    assert session.posts() == []
    session = Session(work)  # Now FINISHED: published without a separate refresh.
    result = finish(work, session)
    [post] = session.posts()
    assert post["url"] == f"{BASE}/{IG}/media_publish"
    assert post["json"] == {"creation_id": CONTAINER}
    assert [r["state"] for r in post["ledger"]] == ["publishing"]
    [record] = result["records"]
    assert record["state"] == "published" and record["media_id"] == MEDIA
    assert record["permalink"] == "https://www.instagram.com/reel/AbC_1/"
    assert finish(work, Session(work))["items"][0]["action"] == "skip"


def test_finish_rechecks_container_before_publishing(work: Path) -> None:
    publish(work, Session(work))
    instagram.status(work, client(Session(work)))
    session = Session(work)
    session.reply("GET", f"{BASE}/{CONTAINER}", Response(200, {"status_code": "EXPIRED"}))
    finish(work, session)
    assert session.posts() == [] and records(work)[0]["state"] == "expired"


def test_lost_publish_response_is_resolved_by_status_not_retried(work: Path) -> None:
    publish(work, Session(work))
    instagram.status(work, client(Session(work)))
    session = Session(work)
    session.reply("POST", f"{BASE}/{IG}/media_publish", requests.Timeout("t"))
    with pytest.raises(ValueError, match="Publish outcome unknown"):
        finish(work, session)
    assert records(work)[0]["state"] == "publishing"
    # FINISHED after a lost publish stays unresolved; finish never posts again.
    instagram.status(work, client(Session(work)))
    assert records(work)[0]["state"] == "publishing"
    again = Session(work)
    finish(work, again)
    assert again.posts() == []
    session = Session(work)
    session.reply("GET", f"{BASE}/{CONTAINER}", Response(200, {"status_code": "PUBLISHED"}))
    instagram.status(work, client(session))
    [record] = records(work)
    assert record["state"] == "api_published" and record["media_id"] is None


def test_finish_requires_source_to_match_approval(work: Path) -> None:
    publish(work, Session(work))
    instagram.status(work, client(Session(work)))
    (work / "out" / "clip-01.mp4").write_bytes(b"edited")
    with pytest.raises(ValueError, match="approval"):
        finish(work, Session(work))


def test_finish_rejects_receipt_for_another_username(work: Path) -> None:
    publish(work, Session(work))
    before = (work / "out" / instagram.LEDGER).read_text()
    with pytest.raises(ValueError, match="another username"):
        instagram.finish_plan(
            work,
            {"clip-01"},
            IG,
            "someone_else",
        )
    assert (work / "out" / instagram.LEDGER).read_text() == before


def test_unsafe_permalink_is_dropped(work: Path) -> None:
    publish(work, Session(work))
    instagram.status(work, client(Session(work)))
    session = Session(work)
    session.reply(
        "GET",
        f"{BASE}/{MEDIA}",
        Response(200, {"id": MEDIA, "permalink": "https://evil.example/r"}),
    )
    assert finish(work, session)["records"][0]["permalink"] is None


def test_prior_browser_publication_is_respected(work: Path) -> None:
    sha = queue.load(work)[0]["approved_sha256"]
    (work / "publications.json").write_text(
        json.dumps([{"platform": "instagram", "sha256": sha, "status": "published"}])
    )
    [item] = instagram.plan(work, {"clip-01"}, IG, USER, False)
    assert item["action"] == "skip" and "publications.json" in item["reason"]
    (work / "publications.json").write_text(
        json.dumps([{"platform": "instagram", "sha256": sha, "status": "in review"}])
    )
    with pytest.raises(ValueError, match="unresolved"):
        instagram.plan(work, {"clip-01"}, IG, USER, False)


def test_tampered_ledger_fails_closed(work: Path) -> None:
    (work / "out" / instagram.LEDGER).write_text(json.dumps({"version": 1, "records": [{}]}))
    with pytest.raises(ValueError, match="invalid record"):
        instagram.plan(work, {"clip-01"}, IG, USER, False)


def test_token_file_must_be_private_and_not_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(instagram.TOKEN_ENV, raising=False)
    path = tmp_path / instagram.TOKEN_FILE
    path.write_text(TOKEN)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        instagram.load_token(tmp_path)
    path.chmod(0o600)
    assert instagram.load_token(tmp_path) == TOKEN
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        instagram.load_token(tmp_path)
    monkeypatch.setenv(instagram.TOKEN_ENV, "short")
    with pytest.raises(ValueError, match="malformed"):
        instagram.load_token(tmp_path)


def test_default_session_ignores_environment_and_netrc() -> None:
    assert instagram.Client(TOKEN).session.trust_env is False


def test_redirect_is_not_followed(work: Path) -> None:
    session = Session(work)
    session.reply("GET", f"{BASE}/{IG}", Response(302, {}))
    with pytest.raises(ValueError, match="Unexpected"):
        publish(work, session, execute=False)


def test_unsupported_sample_rate_fails_before_auth(work: Path, fake_probe: dict[str, Any]) -> None:
    """A 96 kHz render is rejected offline: no token use, request or receipt."""
    fake_probe["audio_sample_rate"] = 96000
    session = Session(work)
    with pytest.raises(ValueError, match="48000 Hz AAC"):
        publish(work, session, execute=False)
    assert session.calls == [] and not (work / "out" / instagram.LEDGER).exists()


def test_finished_outputs_are_terminal_and_offer_no_empty_next_step(work: Path) -> None:
    publish(work, Session(work))
    result = finish(work, Session(work))
    assert result["items"][0]["state"] == "published"
    assert result["items"][0]["action"] == "done"
    repeat_session = Session(work)
    again = publish(work, repeat_session)
    assert again["next"] == [] and again["records"] == []
    assert finish(work, repeat_session)["items"][0]["action"] == "skip"
    assert repeat_session.posts() == []


def test_next_hint_preserves_workspace_with_shell_characters() -> None:
    import shlex

    work = Path("/work/a b;literal")
    hints = instagram.next_commands(work, IG, USER, ["clip-01"])
    assert all(shlex.split(hint)[3] == str(work) for hint in hints)
