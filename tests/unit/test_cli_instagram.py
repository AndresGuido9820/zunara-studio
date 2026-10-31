"""CLI boundary of `zunara instagram`: help, offline doctor, argument checks and policy order."""

import hashlib
import json
from pathlib import Path

import pytest

from zunara import cli, instagram

TOKEN = "EAAcli-synthetic-token-0123456789"  # gitleaks:allow -- synthetic test credential
PROBE = {
    "duration_s": 9.0,
    "width": 1080,
    "height": 1920,
    "codec": "h264",
    "has_audio": True,
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
}
IDS = ["--account-id", "17841400000000001", "--username", "studio.clips"]


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    path.mkdir()
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(path))
    monkeypatch.setenv("ZUNARA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(instagram.TOKEN_ENV, raising=False)
    return path


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("The command must not open a client.")

    monkeypatch.setattr(instagram, "Client", refuse)
    monkeypatch.setattr(instagram, "probe", lambda path: dict(PROBE))


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "session-01"
    (root / "out").mkdir(parents=True)
    content = b"frames"
    (root / "out" / "clip-01.mp4").write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    row = {"file": "clip-01.mp4", "title": "T", "description": "", "tags": []}
    row.update(sha256=sha, approved_sha256=sha, topic="music")
    (root / "out" / "queue.json").write_text(json.dumps([row]))
    return root


def call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    try:
        cli.main(list(argv))
        code = 0
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    out, err = capsys.readouterr()
    return code, out, err


def test_help_lists_instagram_commands(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = call(capsys, "instagram", "--help")
    assert code == 0
    for name in ("doctor", "account", "publish", "status", "finish"):
        assert name in out
    code, out, _ = call(capsys, "instagram", "publish", "--help")
    assert code == 0 and "--ai-generated" in out and "--execute" in out


def test_doctor_is_offline_and_never_prints_the_token(
    config: Path,
    no_network: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = call(capsys, "instagram", "doctor")
    result = json.loads(out)
    assert code == 0 and result["token_source"] is None
    assert result["live_auth"].startswith("unknown") and result["api_version"] == "v26.0"
    monkeypatch.setenv(instagram.TOKEN_ENV, TOKEN)
    code, out, _ = call(capsys, "instagram", "doctor")
    assert code == 0 and json.loads(out)["token_source"] == "environment"
    assert TOKEN not in out


def test_invalid_account_arguments_fail_before_token(
    config: Path, no_network: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = call(capsys, "instagram", "account", "--account-id", "abc", "--username", "x")
    assert code == 2 and "numeric" in err
    code, _, err = call(capsys, "instagram", "account", *IDS, "--api-version", "latest")
    assert code == 2 and "api-version" in err


def test_topic_policy_blocks_before_token_is_read(
    config: Path, no_network: None, work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = {"platforms": {"instagram": {"allowed_topics": ["football"]}}}
    (config / "publishing-policy.json").write_text(json.dumps(policy))
    code, _, err = call(capsys, "instagram", "publish", str(work), "--only", "clip-01", *IDS)
    assert code == 2 and "Topic policy for instagram" in err
    assert "access token" not in err


def test_publish_without_token_fails_cleanly(
    config: Path, no_network: None, work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = call(capsys, "instagram", "publish", str(work), "--only", "clip-01", *IDS)
    assert code == 2 and "No Instagram access token" in err and "Traceback" not in err


def test_status_opens_no_client_and_finish_preview_needs_a_token(
    config: Path, no_network: None, work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = call(capsys, "instagram", "status", str(work))
    assert code == 0 and json.loads(out)["records"] == []
    # The finish preview reads live identity and container status (GET only).
    code, _, err = call(capsys, "instagram", "finish", str(work), "--only", "clip-01", *IDS)
    assert code == 2 and "No Instagram access token" in err
    assert not (work / "out" / instagram.LEDGER).exists()


def test_96khz_audio_is_rejected_before_token(
    config: Path,
    no_network: None,
    work: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(instagram, "probe", lambda path: {**PROBE, "audio_sample_rate": 96000})
    code, _, err = call(capsys, "instagram", "publish", str(work), "--only", "clip-01", *IDS)
    assert code == 2 and "48000 Hz AAC" in err and "access token" not in err
