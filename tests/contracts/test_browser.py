"""Browser publisher contract against a scripted fake UI. No browser, network or account."""

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from zunara import browser, cli, instagram, queue, social

pytestmark = pytest.mark.contract

USER = "studio.clips"
PROBE = {
    "duration_s": 12.0,
    "width": 1080,
    "height": 1920,
    "codec": "h264",
    "has_audio": True,
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
}


class FakeUI:
    """Scripted page operations; snapshots the ledger at each irreversible step."""

    def __init__(self, work: Path, **script: Any) -> None:
        self.work = work
        self.script = script
        self.calls: list[str] = []
        self.attached: list[bytes] = []
        self.captions: list[str] = []
        self.ledger_at: dict[str, Any] = {}

    def _ledger(self) -> Any:
        return json.loads((self.work / "out" / browser.LEDGER).read_text(encoding="utf-8"))

    def _fail(self, step: str) -> None:
        error = self.script.get("fail", {}).get(step)
        if error is not None:
            raise error

    def open(self, url: str) -> None:
        self.calls.append(f"open:{url}")

    def blocker(self) -> str | None:
        return self.script.get("blocker")

    def active_username(self, timeout: float) -> str | None:
        return self.script.get("username", USER)

    def open_composer(self) -> None:
        self.calls.append("composer")
        self._fail("composer")

    def attach(self, path: Path) -> None:
        self.ledger_at["attach"] = self._ledger()
        self.attached.append(path.read_bytes())
        self.calls.append("attach")
        self._fail("attach")

    def keep_original_aspect(self) -> None:
        self.calls.append("aspect")
        self._fail("aspect")

    def advance_to_caption(self) -> None:
        self.calls.append("advance")

    def set_caption(self, text: str) -> None:
        self.captions.append(text)

    def share(self) -> None:
        self.ledger_at["share"] = self._ledger()
        self.calls.append("share")
        self._fail("share")

    def wait_for_outcome(self, timeout: float) -> tuple[str, str | None]:
        outcomes = self.script.get("outcomes", [("published", None)])
        return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]

    def pause(self, seconds: float) -> None:
        self.calls.append("pause")


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "session-01"
    (root / "out").mkdir(parents=True)
    return root


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return tmp_path / "config"


@pytest.fixture
def ready(work: Path, config: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(instagram, "probe", lambda _path: dict(PROBE))
    for stem in ("clip-01", "clip-02"):
        (work / "out" / f"{stem}.mp4").write_bytes(f"video {stem}".encode())
    queue.refresh(work)
    rows = queue.load(work)
    for row in rows:
        row.update(title=f"Title {row['file']}", description=f"Caption #{row['file'][:-4]}")
    queue.save(work, rows)
    queue.approve(work, {"clip-01", "clip-02"})
    browser.profile_dir(config, "instagram", USER, create=True)
    return work


def connect_with(ui: FakeUI) -> browser.Connect:
    @contextlib.contextmanager
    def session(profile: Path, headed: bool) -> Iterator[browser.InstagramUI]:
        ui.calls.append(f"launch:{profile.name}:{headed}")
        yield ui

    return session


def run(work: Path, config: Path, ui: FakeUI, only: set[str], **kwargs: Any) -> dict[str, Any]:
    items = browser.plan(work, only, USER, retry_unshared=kwargs.pop("retry_unshared", False))
    return browser.execute(
        work,
        config,
        items,
        username=USER,
        headed=False,
        share_timeout=30,
        connect=connect_with(ui),
        **kwargs,
    )


def records(work: Path) -> list[dict[str, Any]]:
    return browser.load_ledger(work)["records"]


def test_publishes_snapshot_with_receipts_before_attach_and_share(ready: Path, config: Path):
    ui = FakeUI(ready)
    ui.script["outcomes"] = [("published", "https://www.instagram.com/reel/Abc123/")]
    result = run(ready, config, ui, {"clip-01"})
    assert ui.attached == [b"video clip-01"]
    assert ui.captions == ["Caption #clip-01"]
    assert ui.calls.index("aspect") < ui.calls.index("advance")
    before_attach = ui.ledger_at["attach"]["records"][0]
    assert (before_attach["state"], before_attach["media_selected"]) == ("composing", True)
    before_share = ui.ledger_at["share"]["records"][0]
    assert (before_share["state"], before_share["share_clicked"]) == ("sharing", True)
    [record] = result["records"]
    assert record["state"] == "published"
    assert record["permalink"] == "https://www.instagram.com/reel/Abc123/"
    assert record["aspect"] == "original"
    path = ready / "out" / browser.LEDGER
    assert not stat.S_IMODE(path.stat().st_mode) & 0o077


def test_repeated_run_does_not_publish_twice(ready: Path, config: Path):
    run(ready, config, FakeUI(ready), {"clip-01"})
    again = FakeUI(ready)
    result = run(ready, config, again, {"clip-01"})
    assert result["mode"] == "nothing-to-do"
    assert result["items"][0]["action"] == "skip"
    assert again.calls == []


def test_multiple_items_navigate_and_verify_before_each(ready: Path, config: Path):
    ui = FakeUI(ready)
    result = run(ready, config, ui, {"clip-01", "clip-02"})
    assert [r["state"] for r in result["records"]] == ["published", "published"]
    assert ui.calls.count(f"open:{browser.HOME}") == 2
    assert ui.attached == [b"video clip-01", b"video clip-02"]


def test_later_failure_keeps_earlier_success(ready: Path, config: Path):
    ui = FakeUI(ready, outcomes=[("published", None), ("timeout", None)])
    with pytest.raises(ValueError, match="unknown"):
        run(ready, config, ui, {"clip-01", "clip-02"})
    assert [r["state"] for r in records(ready)] == ["published", "unknown"]


def test_account_mismatch_stops_before_upload_and_allows_rerun(ready: Path, config: Path):
    ui = FakeUI(ready, username="someone.else")
    with pytest.raises(ValueError, match="account_mismatch.*Nothing was attached"):
        run(ready, config, ui, {"clip-01"})
    assert "attach" not in ui.calls
    [record] = records(ready)
    assert (record["state"], record["status"], record["media_selected"]) == (
        "stopped",
        "account_mismatch",
        False,
    )
    assert browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)[0]["action"] == "publish"
    assert run(ready, config, FakeUI(ready), {"clip-01"})["records"][0]["state"] == "published"


@pytest.mark.parametrize("blocker", ["login_required", "checkpoint_required"])
def test_login_or_captcha_stops_before_upload(ready: Path, config: Path, blocker: str):
    ui = FakeUI(ready, blocker=blocker)
    with pytest.raises(ValueError, match=blocker):
        run(ready, config, ui, {"clip-01", "clip-02"})
    assert "composer" not in ui.calls and ui.attached == []
    assert [(r["state"], r["status"]) for r in records(ready)] == [
        ("stopped", blocker),
        ("stopped", "not_attempted"),
    ]
    assert browser.plan(ready, {"clip-02"}, USER, retry_unshared=False)[0]["action"] == "publish"


def test_timeout_after_share_is_unknown_and_blocks_retry(ready: Path, config: Path):
    ui = FakeUI(ready, outcomes=[("timeout", None)])
    with pytest.raises(ValueError, match="will not be retried"):
        run(ready, config, ui, {"clip-01"})
    [record] = records(ready)
    assert (record["state"], record["observed"]) == ("unknown", "timeout")
    item = browser.plan(ready, {"clip-01"}, USER, retry_unshared=True)[0]
    assert item["action"] == "blocked"
    again = FakeUI(ready)
    with pytest.raises(ValueError, match="unresolved browser receipts"):
        run(ready, config, again, {"clip-01"}, retry_unshared=True)
    assert again.calls == []


def test_blocker_seen_after_share_is_unknown(ready: Path, config: Path):
    ui = FakeUI(ready, outcomes=[("checkpoint_required", None)])
    with pytest.raises(ValueError, match="unknown"):
        run(ready, config, ui, {"clip-01"})
    assert records(ready)[0]["state"] == "unknown"


def test_driver_error_on_share_click_is_unknown_and_sanitized(ready: Path, config: Path):
    secret = RuntimeError("https://www.instagram.com/?token=SECRET page text")
    ui = FakeUI(ready, fail={"share": secret})
    with pytest.raises(ValueError) as caught:
        run(ready, config, ui, {"clip-01"})
    assert "SECRET" not in str(caught.value) and "https://" not in str(caught.value)
    assert records(ready)[0]["state"] == "unknown"


def test_ui_change_after_attach_needs_explicit_retry(ready: Path, config: Path):
    ui = FakeUI(ready, fail={"aspect": browser.BrowserStop("ui_changed", "crop missing")})
    with pytest.raises(ValueError, match="ui_changed"):
        run(ready, config, ui, {"clip-01"})
    [record] = records(ready)
    assert (record["state"], record["media_selected"], record["share_clicked"]) == (
        "stopped",
        True,
        False,
    )
    assert browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)[0]["action"] == "blocked"
    result = run(ready, config, FakeUI(ready), {"clip-01"}, retry_unshared=True)
    assert result["records"][0]["state"] == "published"
    assert len(records(ready)) == 1


def test_ui_change_before_attach_is_rerunnable(ready: Path, config: Path):
    ui = FakeUI(ready, fail={"composer": browser.BrowserStop("ui_changed", "no create")})
    with pytest.raises(ValueError, match="ui_changed"):
        run(ready, config, ui, {"clip-01"})
    assert browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)[0]["action"] == "publish"


def test_changed_media_is_refused_before_browser(ready: Path, config: Path):
    (ready / "out" / "clip-01.mp4").write_bytes(b"edited after approval")
    with pytest.raises(ValueError, match="approval"):
        browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)


def test_media_changed_between_plan_and_execute_is_refused(ready: Path, config: Path):
    items = browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)
    (ready / "out" / "clip-01.mp4").write_bytes(b"swapped")
    ui = FakeUI(ready)
    with pytest.raises(ValueError, match="approval"):
        browser.execute(
            ready, config, items, username=USER, headed=False, share_timeout=30,
            connect=connect_with(ui),
        )  # fmt: skip
    assert ui.attached == []


def test_missing_login_marker_requires_login(work: Path, ready: Path, config: Path):
    (config / browser.PROFILE_ROOT / "instagram" / USER / browser.MARKER).unlink()
    ui = FakeUI(ready)
    with pytest.raises(ValueError, match="login_required"):
        run(ready, config, ui, {"clip-01"})
    assert ui.calls == []


def test_native_and_provider_outcomes_are_honored(ready: Path, config: Path):
    row = queue.load(ready)[0]
    (ready / "out" / instagram.LEDGER).write_text(
        json.dumps(
            {
                "version": 1,
                "records": [
                    {
                        "account_id": "1784",
                        "username": USER,
                        "approved_sha256": row["approved_sha256"],
                        "file": row["file"],
                        "api_version": "v26.0",
                        "state": "processing",
                        "container_id": "18",
                        "fields": {},
                    }
                ],
            }
        )
    )
    item = browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)[0]
    assert item["action"] == "skip" and instagram.LEDGER in item["reason"]
    (ready / "publications.json").write_text(
        json.dumps(
            [
                {
                    "platform": "instagram",
                    "sha256": queue.load(ready)[1]["approved_sha256"],
                    "status": "published",
                }
            ]
        )  # fmt: skip
    )
    item = browser.plan(ready, {"clip-02"}, USER, retry_unshared=False)[0]
    assert item["action"] == "skip" and "publications.json" in item["reason"]


def test_other_adapters_honor_browser_receipts(ready: Path, config: Path):
    run(ready, config, FakeUI(ready), {"clip-01"})
    native = instagram.plan(ready, {"clip-01"}, "1784", USER, False)
    assert native[0]["action"] == "skip" and browser.LEDGER in native[0]["reason"]
    for account in (USER, "17841400000000001", "opaque-provider-id"):
        # A provider account ID cannot be compared with a username; the content hash blocks.
        provider = social.plan(ready, {"clip-01"}, ["instagram"], "profile", {"instagram": account})
        assert provider[0]["action"] == "skip" and browser.LEDGER in provider[0]["reason"]


def test_login_waits_for_manual_login_and_verifies_identity(config: Path):
    ui = FakeUI(Path("."), blocker="login_required")
    ticks = {"n": 0}

    def pause(_seconds: float) -> None:
        ticks["n"] += 1
        ui.script["blocker"] = None  # The person finished logging in.

    ui.pause = pause  # type: ignore[method-assign]
    result = browser.login(config, USER, 30, connect=connect_with(ui))
    assert result["status"] == "connected" and ticks["n"] == 1
    assert ui.calls[0] == f"launch:{USER}:True"
    assert ui.calls[1] == f"open:{browser.LOGIN_PAGE}"
    marker = json.loads((config / browser.PROFILE_ROOT / "instagram" / USER / browser.MARKER)
                        .read_text())  # fmt: skip
    assert "last_verified_login_at" in marker


def test_login_rejects_other_account_and_times_out(config: Path):
    with pytest.raises(ValueError, match="account_mismatch"):
        browser.login(config, USER, 30, connect=connect_with(FakeUI(Path("."), username="x.y")))
    stuck = FakeUI(Path("."), blocker="checkpoint_required")
    with pytest.raises(ValueError, match="login_required.*0.01"):
        browser.login(config, USER, 0.01, connect=connect_with(stuck))
    marker = json.loads((config / browser.PROFILE_ROOT / "instagram" / USER / browser.MARKER)
                        .read_text())  # fmt: skip
    assert "last_verified_login_at" not in marker


def test_cli_preview_is_offline_and_execute_checks_policy(
    ready: Path, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(config))

    def refuse(*_args: Any) -> Any:
        raise AssertionError("browser must not open")

    monkeypatch.setattr(browser, "connect_playwright", refuse)
    base = ["browser", "publish", str(ready), "--platform", "instagram", "--username", USER]
    cli.main([*base, "--only", "clip-01"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["mode"] == "preview" and shown["session"].startswith("unverified")
    assert shown["items"][0]["caption"] == "Caption #clip-01"
    assert not (ready / "out" / browser.LEDGER).exists()
    (config / "publishing-policy.json").write_text(
        json.dumps({"platforms": {"instagram": {"allowed_topics": ["music"]}}})
    )
    with pytest.raises(SystemExit):
        cli.main([*base, "--only", "clip-01", "--execute"])
    assert "Topic policy" in capsys.readouterr().err
    assert not (ready / "out" / browser.LEDGER).exists()


@pytest.mark.parametrize("platform", ["tiktok", "youtube", "x", "facebook"])
def test_cli_rejects_unsupported_platforms(
    work: Path, platform: str, capsys: pytest.CaptureFixture[str]
):
    with pytest.raises(SystemExit):
        cli.main(["browser", "login", "--platform", platform, "--username", USER])
    assert "only instagram" in capsys.readouterr().err


def test_receipt_records_caption_hash_not_profile_secrets(ready: Path, config: Path):
    run(ready, config, FakeUI(ready), {"clip-01"})
    [record] = records(ready)
    assert record["caption_sha256"] == hashlib.sha256(b"Caption #clip-01").hexdigest()
    text = (ready / "out" / browser.LEDGER).read_text()
    assert "cookie" not in text.lower() and str(config) not in text
    assert os.listdir(ready / "out").count(browser.LEDGER) == 1


def run_chrome(work: Path, config: Path, ui: FakeUI, only: set[str]) -> dict[str, Any]:
    items = browser.plan(work, only, USER, retry_unshared=False)
    return browser.execute(
        work,
        config,
        items,
        username=USER,
        headed=False,
        share_timeout=30,
        connect=connect_with(ui),
        connection="chrome",
    )


def test_chrome_connection_reuses_lifecycle_without_profile_marker(work: Path, ready: Path):
    fresh = work.parent / "fresh-config"  # No dedicated profile or login marker exists here.
    ui = FakeUI(ready)
    result = run_chrome(ready, fresh, ui, {"clip-01"})
    [record] = result["records"]
    assert (record["state"], record["connection"], record["headed"]) == (
        "published",
        "chrome",
        None,
    )
    assert result["connection"] == "chrome"
    assert ui.ledger_at["share"]["records"][0]["state"] == "sharing"
    assert not (fresh / browser.PROFILE_ROOT / "instagram" / USER).exists()
    again = FakeUI(ready)
    assert run(ready, fresh, again, {"clip-01"})["mode"] == "nothing-to-do"  # Either connection.


@pytest.mark.parametrize("blocker", ["login_required", "checkpoint_required"])
def test_chrome_stops_point_to_main_chrome(ready: Path, config: Path, blocker: str):
    ui = FakeUI(ready, blocker=blocker)
    with pytest.raises(ValueError) as caught:
        run_chrome(ready, config, ui, {"clip-01"})
    message = str(caught.value)
    assert blocker in message and "connection chrome" in message and "main Chrome" in message
    assert "browser connect" in message and "browser login" not in message
    assert ui.attached == []


def test_chrome_account_mismatch_is_mandatory(ready: Path, config: Path):
    ui = FakeUI(ready, username="someone.else")
    with pytest.raises(ValueError, match="account_mismatch.*main Chrome"):
        run_chrome(ready, config, ui, {"clip-01"})
    assert ui.attached == []


def test_chrome_bridge_lock_blocks_overlap(ready: Path, config: Path):
    from zunara import browser_bridge

    with browser_bridge.session_lock(config), pytest.raises(ValueError, match="in use"):
        run_chrome(ready, config, FakeUI(ready), {"clip-01"})


def test_cli_chrome_preview_is_offline_and_keeps_connection(
    ready: Path, config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from zunara import browser_bridge

    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(config))

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no CLI call in preview")

    monkeypatch.setattr(browser_bridge.Cli, "call", refuse)
    base = ["browser", "publish", str(ready), "--platform", "instagram", "--username", USER]
    cli.main([*base, "--only", "clip-01", "--connection", "chrome"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["connection"] == "chrome" and shown["bridge"].startswith("unverified")
    assert "--connection chrome --execute" in shown["next"][0]
    with pytest.raises(SystemExit):
        cli.main([*base, "--only", "clip-01", "--connection", "chrome", "--headed"])
    assert "dedicated profile" in capsys.readouterr().err
    cli.main(["browser", "doctor"])
    report = json.loads(capsys.readouterr().out)
    assert report["chrome_bridge"]["cli_package"] == "@playwright/cli@0.1.22"
    assert report["chrome_bridge"]["bridge_connection"].startswith("not verified")


def test_chrome_bridge_setup_failure_persists_stopped_receipts(
    ready: Path, config: Path, monkeypatch: pytest.MonkeyPatch
):
    import subprocess

    from zunara import browser_bridge

    monkeypatch.delenv(browser_bridge.TOKEN_ENV, raising=False)

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "not json https://x.test/secret", "")

    bridge = browser_bridge.Cli(config, runner=runner, command=["playwright-cli"])
    items = browser.plan(ready, {"clip-01"}, USER, retry_unshared=False)
    with pytest.raises(ValueError) as caught:
        browser.execute(
            ready,
            config,
            items,
            username=USER,
            headed=False,
            share_timeout=30,
            connection="chrome",
            connect=browser_bridge.connect_chrome(config, cli=bridge),
        )
    assert "connection_required" in str(caught.value) and "https://" not in str(caught.value)
    assert "browser connect --platform instagram --username studio.clips" in str(caught.value)
    [record] = records(ready)
    assert (record["state"], record["status"], record["media_selected"]) == (
        "stopped",
        "connection_required",
        False,
    )
