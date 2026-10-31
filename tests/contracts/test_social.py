"""Upload-Post adapter contract, exercised against a fake HTTP session. Nothing reaches the network."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

from zunara import queue, social

pytestmark = pytest.mark.contract

KEY = "test-key-0123456789"  # gitleaks:allow -- synthetic contract-test credential
PROFILE = "studio"
ACCOUNTS = {
    "instagram": {"username": "ig_handle", "display_name": "Person", "social_images": "https://x"},
    "tiktok": {"handle": "tt_handle", "reauth_required": False},
    "youtube": {"username": "UCabcdefghijklmnopqrstuv"},
    "x": "x_handle",
}
EXPECTED = {
    "instagram": "ig_handle",
    "tiktok": "tt_handle",
    "youtube": "UCabcdefghijklmnopqrstuv",
    "x": "x_handle",
}


class Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self.body = body

    def json(self) -> Any:
        if self.body is None:
            raise ValueError("not json")
        return self.body


class Session:
    """Records every request; replies from scripted ``(method, path)`` queues."""

    def __init__(self, work: Path | None = None, plan: str = "Basic") -> None:
        self.work = work
        self.calls: list[dict[str, Any]] = []
        self.ledger_at_post: list[Any] = []
        self.replies: dict[tuple[str, str], list[Any]] = {
            ("GET", "/uploadposts/me"): [Response(200, {"success": True, "plan": plan})],
            ("GET", f"/uploadposts/users/{PROFILE}"): [
                Response(200, {"profile": {"username": PROFILE, "social_accounts": ACCOUNTS}})
            ],
        }

    def reply(self, method: str, path: str, *responses: Any) -> None:
        self.replies[(method, path)] = list(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        path = url.removeprefix(social.API)
        call = {"method": method, "path": path, **kwargs}
        if "files" in kwargs:
            call["video"] = kwargs["files"]["video"][1].read()
            ledger = self.work / "out" / social.LEDGER if self.work else None
            self.ledger_at_post.append(json.loads(ledger.read_text()) if ledger else None)
        self.calls.append(call)
        queue_ = self.replies[(method, path)]
        step = queue_.pop(0) if len(queue_) > 1 else queue_[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def posts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


def approved(work: Path, name: str = "clip-01", content: bytes = b"video-bytes", **row: Any) -> str:
    (work / "out").mkdir(parents=True, exist_ok=True)
    (work / "out" / f"{name}.mp4").write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    rows = json.loads((work / "out" / "queue.json").read_text()) if row.get("_append") else []
    row.pop("_append", None)
    rows.append(
        {
            "file": f"{name}.mp4",
            "title": "A short title",
            "description": "The reviewed caption",
            "tags": [],
            "sha256": sha,
            "approved_sha256": sha,
            "youtube_id": None,
            "upload_state": "pending",
            **row,
        }
    )
    (work / "out" / "queue.json").write_text(json.dumps(rows))
    return sha


def run(
    work: Path,
    session: Session,
    platforms: list[str],
    *,
    execute: bool = True,
    schedule: datetime | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    expected = {p: EXPECTED[p] for p in platforms}
    items = social.plan(work, only or {"clip-01"}, platforms, PROFILE, expected)
    return social.publish(
        work,
        social.Client(KEY, session),  # type: ignore[arg-type]
        items,
        profile=PROFILE,
        expected=expected,
        schedule=schedule,
        privacy="private",
        language="es",
        execute=execute,
    )


def ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "out" / social.LEDGER).read_text())["records"]


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "session-01"
    approved(root)
    return root


def test_preview_checks_identity_and_uploads_nothing(work: Path) -> None:
    session = Session(work)
    result = run(work, session, ["instagram", "youtube"], execute=False)
    assert result["mode"] == "preview" and result["plan"] == "Basic"
    assert [c["path"] for c in session.calls] == [
        "/uploadposts/me",
        f"/uploadposts/users/{PROFILE}",
    ]
    assert all(c["allow_redirects"] is False for c in session.calls)
    assert not (work / "out" / social.LEDGER).exists()


def test_execute_sends_snapshot_and_records_before_post(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(200, {"success": True, "request_id": "req-1"}))
    run(work, session, ["instagram", "tiktok", "youtube"])
    [post] = session.posts()
    assert post["video"] == b"video-bytes"
    form = post["data"]
    assert [v for k, v in form if k == "platform[]"] == ["instagram", "tiktok", "youtube"]
    assert ("async_upload", "true") in form and ("user", PROFILE) in form
    assert ("instagram_title", "The reviewed caption") in form
    assert ("tiktok_title", "The reviewed caption") in form
    assert ("youtube_title", "A short title") in form and ("privacyStatus", "private") in form
    assert ("media_type", "REELS") in form and ("post_mode", "DIRECT_POST") in form
    assert ("defaultLanguage", "es") in form
    assert (
        post["auth"](requests.Request("POST", social.API).prepare()).headers["Authorization"]
        == f"Apikey {KEY}"
    )
    [before] = session.ledger_at_post
    assert {r["state"] for r in before["records"]} == {"submitting"}
    records = ledger(work)
    assert {(r["platform"], r["state"], r["request_id"]) for r in records} == {
        (p, "accepted", "req-1") for p in ("instagram", "tiktok", "youtube")
    }
    assert records[0]["account"] == "ig_handle"
    assert oct(os.stat(work / "out" / social.LEDGER).st_mode & 0o777) == "0o600"


def test_repeated_execute_does_not_post_again(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(200, {"request_id": "req-1"}))
    run(work, session, ["instagram"])
    again = social.plan(work, {"clip-01"}, ["instagram"], PROFILE, {"instagram": "ig_handle"})
    assert again[0]["action"] == "skip" and "accepted" in again[0]["reason"]


@pytest.mark.parametrize(
    "failure",
    [requests.Timeout("slow"), Response(502, None), Response(200, None), Response(200, {"ok": 1})],
)
def test_uncertain_outcome_stays_unknown_and_blocks_repeat(work: Path, failure: Any) -> None:
    session = Session(work)
    session.reply("POST", "/upload", failure)
    with pytest.raises(ValueError, match="outcome unknown"):
        run(work, session, ["instagram"])
    assert [r["state"] for r in ledger(work)] == ["unknown"]
    again = social.plan(work, {"clip-01"}, ["instagram"], PROFILE, {"instagram": "ig_handle"})
    assert again[0]["action"] == "skip"


def test_redirect_is_not_followed(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(302, None))
    with pytest.raises(ValueError, match="outcome unknown"):
        run(work, session, ["instagram"])


def test_definite_rejection_releases_records_without_leaking_key(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(401, {"message": f"bad key {KEY}"}))
    with pytest.raises(ValueError) as error:
        run(work, session, ["instagram"])
    assert KEY not in str(error.value) and "API key" in str(error.value)
    assert ledger(work) == []


@pytest.mark.parametrize(
    ("account", "message"),
    [
        ({"username": "someone_else"}, "does not match"),
        ({"display_name": "ig_handle"}, "No instagram account"),
        (None, "No instagram account"),
        ({"username": "ig_handle", "reauth_required": True}, "reauthorization"),
    ],
)
def test_account_binding_prevents_post(work: Path, account: Any, message: str) -> None:
    session = Session(work)
    session.reply(
        "GET",
        f"/uploadposts/users/{PROFILE}",
        Response(
            200, {"profile": {"username": PROFILE, "social_accounts": {"instagram": account}}}
        ),
    )
    with pytest.raises(ValueError, match=message):
        run(work, session, ["instagram"])
    assert session.posts() == [] and not (work / "out" / social.LEDGER).exists()


@pytest.mark.parametrize("plan", ["Free", "free", None, "<script>"])
def test_tiktok_needs_a_known_paid_plan(work: Path, plan: str | None) -> None:
    session = Session(work, plan=plan)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="TikTok"):
        run(work, session, ["tiktok"])
    assert session.posts() == []


def test_free_plan_allows_other_platforms_in_preview(work: Path) -> None:
    assert run(work, Session(work, plan="Free"), ["instagram"], execute=False)["plan"] == "Free"


def test_changed_media_is_refused_before_any_request(work: Path) -> None:
    (work / "out" / "clip-01.mp4").write_bytes(b"edited")
    session = Session(work)
    with pytest.raises(ValueError, match="changed after approval"):
        run(work, session, ["instagram"])
    assert session.calls == []


def test_media_changed_during_snapshot_is_not_recorded_or_sent(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(work)
    original = queue.copy_approved

    def tampered(work_: Path, row: dict[str, Any], destination: Path) -> Path:
        (work_ / "out" / row["file"]).write_bytes(b"swapped")
        return original(work_, row, destination)

    monkeypatch.setattr(queue, "copy_approved", tampered)
    with pytest.raises(ValueError, match="changed"):
        run(work, session, ["instagram"])
    assert session.posts() == [] and not (work / "out" / social.LEDGER).exists()


def test_sync_partial_results(work: Path) -> None:
    session = Session(work)
    results = {
        "instagram": {"success": True, "url": "https://www.instagram.com/reel/abc/"},
        "youtube": {"success": False, "error": "quota"},
    }
    session.reply("POST", "/upload", Response(200, {"success": True, "results": results}))
    run(work, session, ["instagram", "youtube"])
    states = {r["platform"]: r for r in ledger(work)}
    assert states["instagram"]["state"] == "completed"
    assert states["instagram"]["post_url"] == "https://www.instagram.com/reel/abc/"
    assert states["youtube"]["state"] == "failed"


def test_status_refresh_parses_list_results_with_gets_only(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(200, {"request_id": "req-9"}))
    run(work, session, ["tiktok", "youtube"])
    session.reply(
        "GET",
        "/uploadposts/status",
        Response(
            200,
            {
                "status": "completed",
                "results": [
                    {
                        "platform": "tiktok",
                        "success": True,
                        "post_url": None,
                        "warnings": ["later"],
                    },
                    {"platform": "youtube", "success": True, "platform_post_id": "dQw4w9WgXcQ"},
                ],
            },
        ),
    )
    before = len(session.posts())
    records = {
        r["platform"]: r for r in social.status(work, social.Client(KEY, session))["records"]
    }  # type: ignore[arg-type]
    assert records["tiktok"]["state"] == "accepted"
    assert records["youtube"]["state"] == "completed"
    assert records["youtube"]["platform_post_id"] == "dQw4w9WgXcQ"
    assert len(session.posts()) == before
    assert session.calls[-1]["params"] == {"request_id": "req-9"}


def test_schedule_is_sent_in_utc_and_recorded_by_job(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(202, {"success": True, "job_id": "job-7"}))
    now = datetime.now(UTC)
    moment = social.parse_schedule((now + timedelta(days=2)).astimezone().isoformat(), now)
    run(work, session, ["instagram"], schedule=moment)
    form = dict(session.posts()[0]["data"])
    assert form["scheduled_date"].endswith("Z")
    assert ledger(work)[0]["state"] == "scheduled" and ledger(work)[0]["job_id"] == "job-7"


@pytest.mark.parametrize("value", ["2030-01-01T12:00:00", "2000-01-01T00:00:00+00:00"])
def test_schedule_requires_future_offset(value: str) -> None:
    with pytest.raises(ValueError, match="Schedule"):
        social.parse_schedule(value, datetime.now(UTC))


def test_schedule_horizon_is_365_days() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="365"):
        social.parse_schedule((now + timedelta(days=400)).isoformat(), now)


def test_existing_youtube_id_and_browser_record_skip_destinations(tmp_path: Path) -> None:
    work = tmp_path / "w"
    approved(work, youtube_id="abcdefghijk", upload_state="uploaded")
    (work / "publications.json").write_text(
        json.dumps([{"file": "clip-01.mp4", "platform": "instagram", "state": "published"}])
    )
    items = social.plan(work, {"clip-01"}, ["youtube", "instagram"], PROFILE, EXPECTED)
    assert [i["action"] for i in items] == ["skip", "skip"]


def test_unresolved_browser_record_fails_closed(tmp_path: Path) -> None:
    work = tmp_path / "w"
    approved(work)
    (work / "publications.json").write_text(
        json.dumps([{"file": "clip-01.mp4", "platform": "x", "state": "uploading"}])
    )
    with pytest.raises(ValueError, match="unresolved"):
        social.plan(work, {"clip-01"}, ["x"], PROFILE, EXPECTED)


def test_accounts_output_drops_personal_payload() -> None:
    session = Session()
    body = {
        "plan": "Basic",
        "profiles": [{"username": PROFILE, "email": "a@b.c", "social_accounts": ACCOUNTS}],
    }
    session.reply("GET", "/uploadposts/users", Response(200, body))
    result = social.accounts(social.Client(KEY, session))  # type: ignore[arg-type]
    text = json.dumps(result)
    assert "a@b.c" not in text and "Person" not in text and "social_images" not in text
    assert result["profiles"][0]["accounts"]["x"]["username"] == "x_handle"


def test_key_file_must_be_private_and_not_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(social.KEY_ENV, raising=False)
    key = tmp_path / social.KEY_FILE
    key.write_text(KEY)
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private") as error:
        social.load_key(tmp_path)
    assert KEY not in str(error.value)
    key.chmod(0o600)
    assert social.load_key(tmp_path) == KEY
    link = tmp_path / "linked"
    link.mkdir()
    (link / social.KEY_FILE).symlink_to(key)
    with pytest.raises(ValueError, match="symlink"):
        social.load_key(link)
    monkeypatch.setenv(social.KEY_ENV, "env-key-0123456789")  # gitleaks:allow -- synthetic fixture
    assert social.load_key(link) == "env-key-0123456789"


def test_platform_keyed_browser_log_is_respected_and_kept(tmp_path: Path) -> None:
    work = tmp_path / "w"
    sha = approved(work)
    log = {
        "youtube": {
            "status": "published",
            "url": "https://www.youtube.com/shorts/EXAMPLE",
            "file": "clip-01.mp4",
            "sha256": sha,
        },
        "instagram": {"status": "exported", "published": False},
    }
    (work / "publications.json").write_text(json.dumps(log))
    items = social.plan(work, {"clip-01"}, ["youtube", "instagram"], PROFILE, EXPECTED)
    assert [(i["platform"], i["action"]) for i in items] == [
        ("youtube", "skip"),
        ("instagram", "upload"),
    ]
    assert json.loads((work / "publications.json").read_text()) == log


def test_keyed_record_without_file_is_inferred_only_for_a_single_clip(tmp_path: Path) -> None:
    work = tmp_path / "w"
    approved(work)
    (work / "publications.json").write_text(json.dumps({"x": {"published": True}}))
    assert social.plan(work, {"clip-01"}, ["x"], PROFILE, EXPECTED)[0]["action"] == "skip"
    approved(work, "clip-02", b"other", _append=True)
    with pytest.raises(ValueError, match="without file or sha256"):
        social.plan(work, {"clip-01"}, ["x"], PROFILE, EXPECTED)


def test_ledger_symlink_is_refused(work: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    (work / "out" / social.LEDGER).symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        social.plan(work, {"clip-01"}, ["instagram"], PROFILE, EXPECTED)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/reel/abc/",
        "https://www.instagram.com/reel/abc/?sig=secret",
        "https://user:pw@www.instagram.com/reel/abc/",
        "http://www.instagram.com/reel/abc/",
    ],
)
def test_untrusted_result_urls_are_not_recorded(work: Path, url: str) -> None:
    session = Session(work)
    results = {"instagram": {"success": True, "url": url}}
    session.reply("POST", "/upload", Response(200, {"results": results}))
    run(work, session, ["instagram"])
    [record] = ledger(work)
    assert record["state"] == "accepted" and "post_url" not in record


def test_duplicate_skip_reports_the_recorded_account(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(200, {"request_id": "req-1"}))
    run(work, session, ["instagram"])
    [item] = social.plan(work, {"clip-01"}, ["instagram"], PROFILE, {"instagram": "new_handle"})
    assert item["action"] == "skip"
    assert item["recorded_account"] == "ig_handle" and item["account_changed"] is True


@pytest.mark.parametrize("plan", ["Enterprise Plus", "Pro", "Basic-ish"])
def test_unexpected_plan_is_not_assumed_to_include_tiktok(work: Path, plan: str) -> None:
    session = Session(work, plan=plan)
    with pytest.raises(ValueError, match="TikTok needs"):
        run(work, session, ["tiktok"], execute=False)
    assert session.posts() == []


@pytest.mark.parametrize("plan", ["Basic", " professional ", "ADVANCED", "Business"])
def test_known_paid_plans_allow_tiktok_preview(work: Path, plan: str) -> None:
    assert run(work, Session(work, plan=plan), ["tiktok"], execute=False)["mode"] == "preview"


def test_unknown_plan_does_not_gate_instagram_or_youtube(work: Path) -> None:
    result = run(work, Session(work, plan="Something New"), ["instagram", "youtube"], execute=False)
    assert result["plan"] == "Something New"


def test_preview_shows_exact_fields_and_receipt_keeps_request(work: Path) -> None:
    session = Session(work)
    now = datetime.now(UTC)
    moment = social.parse_schedule((now + timedelta(days=1)).isoformat(), now)
    preview = run(work, session, ["instagram", "youtube"], execute=False, schedule=moment)
    shown = {i["platform"]: i for i in preview["items"]}
    assert shown["instagram"]["fields"] == {
        "instagram_title": "The reviewed caption",
        "media_type": "REELS",
    }
    assert shown["youtube"]["fields"] == {
        "youtube_title": "A short title",
        "youtube_description": "The reviewed caption",
        "privacyStatus": "private",
        "defaultLanguage": "es",
    }
    assert shown["youtube"]["scheduled_at"] == moment.isoformat()  # type: ignore[union-attr]
    session.reply("POST", "/upload", Response(202, {"job_id": "job-1"}))
    run(work, session, ["instagram", "youtube"], schedule=moment)
    form = session.posts()[0]["data"]
    for item in shown.values():
        for pair in item["fields"].items():
            assert pair in form
    records = {r["platform"]: r for r in ledger(work)}
    assert records["youtube"]["privacy"] == "private" and records["youtube"]["language"] == "es"
    assert "privacy" not in records["instagram"]
    assert KEY not in (work / "out" / social.LEDGER).read_text()


def test_lost_response_is_resolved_by_saved_request_id_without_reposting(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", requests.Timeout("lost"))
    with pytest.raises(ValueError, match="status --refresh"):
        run(work, session, ["instagram"])
    [post] = session.posts()
    request_id = dict(post["data"])["request_id"]
    assert post["headers"] == {"Idempotency-Key": request_id}
    assert session.ledger_at_post[0]["records"][0]["request_id"] == request_id
    [record] = ledger(work)
    assert record["state"] == "unknown" and record["request_id"] == request_id
    session.reply(
        "GET",
        "/uploadposts/status",
        Response(
            200,
            {
                "request_id": request_id,
                "status": "completed",
                "results": [
                    {
                        "platform": "instagram",
                        "success": True,
                        "post_url": "https://www.instagram.com/reel/abc123/",
                    }
                ],
            },
        ),
    )
    [record] = social.status(work, social.Client(KEY, session))["records"]  # type: ignore[arg-type]
    assert record["state"] == "completed"
    assert session.calls[-1]["params"] == {"request_id": request_id}
    assert len(session.posts()) == 1
    again = social.plan(work, {"clip-01"}, ["instagram"], PROFILE, {"instagram": "ig_handle"})
    assert again[0]["action"] == "skip"


def test_unknown_request_not_found_stays_unknown(work: Path) -> None:
    session = Session(work)
    session.reply("POST", "/upload", Response(503, None))
    with pytest.raises(ValueError, match="outcome unknown"):
        run(work, session, ["instagram"])
    session.reply("GET", "/uploadposts/status", Response(404, None))
    result = social.status(work, social.Client(KEY, session))  # type: ignore[arg-type]
    assert result["records"][0]["state"] == "unknown" and len(result["refresh_errors"]) == 1


def test_tiktok_inbox_fallback_is_a_draft_not_completed(work: Path) -> None:
    session = Session(work)
    results = {
        "tiktok": {
            "success": True,
            "fallback_to_inbox": True,
            "url": "https://www.tiktok.com/@tt_handle/video/123",
        }
    }
    session.reply("POST", "/upload", Response(200, {"success": True, "results": results}))
    run(work, session, ["tiktok"])
    assert ("disable_inbox_fallback", "true") in session.posts()[0]["data"]
    [record] = ledger(work)
    assert record["state"] == "accepted" and record["delivery"] == "draft"
    assert "post_url" not in record


def test_tiktok_inbox_id_prefix_is_a_draft(work: Path) -> None:
    session = Session(work)
    results = {"tiktok": {"success": True, "publish_id": "v_inbox_file~abc.123"}}
    session.reply("POST", "/upload", Response(200, {"success": True, "results": results}))
    run(work, session, ["tiktok"])
    [record] = ledger(work)
    assert record["state"] == "accepted" and record["delivery"] == "draft"


@pytest.mark.parametrize(
    "body",
    [
        {"anything": 1},
        {"status": "weird"},
        {"status": "completed", "success": False},
        {"status": "completed", "request_id": "someone-else"},
    ],
)
def test_unrecognized_status_reply_leaves_unknown_unresolved(work: Path, body: Any) -> None:
    session = Session(work)
    session.reply("POST", "/upload", requests.Timeout("lost"))
    with pytest.raises(ValueError, match="outcome unknown"):
        run(work, session, ["instagram"])
    session.reply("GET", "/uploadposts/status", Response(200, body))
    result = social.status(work, social.Client(KEY, session))  # type: ignore[arg-type]
    assert result["records"][0]["state"] == "unknown"
    assert result["refresh_errors"][0]["error"].startswith("Unrecognized")


def test_keyed_result_cannot_claim_another_platform() -> None:
    records = {
        "instagram": {"state": "accepted"},
        "youtube": {"state": "accepted"},
    }
    social.apply_results(
        records,
        {"instagram": {"platform": "youtube", "success": True, "post_id": "ig-123"}},
    )
    assert records["instagram"]["state"] == "completed"
    assert records["youtube"]["state"] == "accepted"
