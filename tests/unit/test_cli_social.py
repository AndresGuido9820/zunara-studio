"""CLI boundary of `zunara social`: help, offline doctor, argument checks and policy order."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from zunara import cli, social

KEY = "cli-secret-key-0123456789"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(path))
    monkeypatch.setenv("ZUNARA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(social.KEY_ENV, raising=False)
    return path


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("The command must not open a client.")

    monkeypatch.setattr(social, "Client", refuse)


def call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    try:
        cli.main(list(argv))
        code = 0
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    out, err = capsys.readouterr()
    return code, out, err


def test_help_lists_social_commands(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = call(capsys, "social", "--help")
    assert code == 0
    for name in ("doctor", "accounts", "publish", "status"):
        assert name in out


def test_doctor_is_offline_and_never_prints_the_key(
    config: Path,
    no_network: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = call(capsys, "social", "doctor")
    result = json.loads(out)
    assert code == 0 and result["api_key_source"] is None
    assert result["live_auth"].startswith("unknown")
    monkeypatch.setenv(social.KEY_ENV, KEY)
    code, out, _ = call(capsys, "social", "doctor")
    assert json.loads(out)["api_key_source"] == "environment" and KEY not in out


def test_every_platform_needs_an_exact_account(
    config: Path, work: Path, no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = call(
        capsys, "social", "publish", str(work), "--only", "a", "--profile", "p",
        "--platform", "instagram", "--platform", "youtube", "--account", "instagram=me",
    )  # fmt: skip
    assert code == 2 and "every selected --platform" in err


def test_topic_policy_blocks_before_key_or_network(
    config: Path,
    work: Path,
    add_clip: Callable[..., Path],
    write_queue: Callable[[object], Path],
    no_network: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_clip("a.mp4", b"bytes")
    sha = __import__("hashlib").sha256(b"bytes").hexdigest()
    write_queue([{"file": "a.mp4", "title": "T", "sha256": sha, "approved_sha256": sha}])
    (config / "publishing-policy.json").write_text(
        json.dumps({"platforms": {"tiktok": {"allowed_topics": ["clips"]}}})
    )
    code, _, err = call(
        capsys, "social", "publish", str(work), "--only", "a", "--profile", "p",
        "--platform", "tiktok", "--account", "tiktok=me", "--execute",
    )  # fmt: skip
    assert code == 2 and "Topic policy for tiktok" in err
    assert not (work / "out" / social.LEDGER).exists()


def test_missing_key_is_reported_without_network(
    config: Path,
    work: Path,
    add_clip: Callable[..., Path],
    write_queue: Callable[[object], Path],
    no_network: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_clip("a.mp4", b"bytes")
    sha = __import__("hashlib").sha256(b"bytes").hexdigest()
    write_queue([{"file": "a.mp4", "title": "T", "sha256": sha, "approved_sha256": sha}])
    code, _, err = call(
        capsys, "social", "publish", str(work), "--only", "a", "--profile", "p",
        "--platform", "x", "--account", "x=me",
    )  # fmt: skip
    assert code == 2 and social.KEY_ENV in err


def test_status_without_receipts_is_empty(
    config: Path, work: Path, no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = call(capsys, "social", "status", str(work))
    assert code == 0 and json.loads(out) == {"records": []}


def test_nothing_to_do_needs_no_key_and_hides_internal_rows(
    config: Path,
    work: Path,
    add_clip: Callable[..., Path],
    write_queue: Callable[[object], Path],
    no_network: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_clip("a.mp4", b"bytes")
    sha = __import__("hashlib").sha256(b"bytes").hexdigest()
    write_queue(
        [
            {
                "file": "a.mp4",
                "title": "T",
                "sha256": sha,
                "approved_sha256": sha,
                "youtube_id": "abcdefghijk",
                "upload_state": "uploaded",
            }
        ]
    )
    code, out, _ = call(
        capsys, "social", "publish", str(work), "--only", "a", "--profile", "p",
        "--platform", "youtube", "--account", "youtube=me", "--execute",
    )  # fmt: skip
    result = json.loads(out)
    assert code == 0 and result["mode"] == "nothing-to-do" and "_row" not in out
    assert result["items"][0]["action"] == "skip"
