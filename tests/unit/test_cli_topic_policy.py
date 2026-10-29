"""Topic policy at the CLI boundary: export and publish are checked before any side effect."""

import json
from pathlib import Path
from typing import Any

import pytest

from zunara import cli, queue
from zunara.editorial import POLICY_FILE

CHANNEL = "UCabcdefghijklmnopqrstuv"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point every user directory at tmp_path so the real HOME and config are never read."""
    home = tmp_path / "home"
    home.mkdir()
    paths = {"home": home, "config": home / "config", "data": home / "data"}
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(paths["config"]))
    monkeypatch.setenv("ZUNARA_DATA_DIR", str(paths["data"]))
    monkeypatch.delenv("ZUNARA_YOUTUBE_CLIENT_SECRET", raising=False)
    return paths


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "session-01"
    (root / "out").mkdir(parents=True)
    topics = {"code": "programming", "match": "football", "untagged": None}
    for stem in topics:
        (root / "out" / f"{stem}.mp4").write_bytes(f"{stem} bytes".encode())
    rows = queue.refresh(root)
    for row in rows:
        stem = Path(row["file"]).stem
        row["title"] = f"Clip {stem}"
        if topics[stem] is not None:
            row["topic"] = topics[stem]
    queue.save(root, rows)
    queue.approve(root, set(topics))
    return root


def set_policy(env: dict[str, Path], content: object) -> None:
    env["config"].mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (env["config"] / POLICY_FILE).write_text(text, encoding="utf-8")


def x_only_programming(env: dict[str, Path]) -> None:
    set_policy(env, {"platforms": {"x": {"allowed_topics": ["programming"]}}})


def run(capsys: pytest.CaptureFixture[str], *argv: str | Path) -> tuple[int, Any, str]:
    try:
        cli.main([str(a) for a in argv])
    except SystemExit as exit_:
        captured = capsys.readouterr()
        return int(exit_.code or 0), None, captured.err
    captured = capsys.readouterr()
    return 0, json.loads(captured.out), captured.err


# Export


def test_without_policy_export_is_unrestricted(
    env: dict[str, Path], work: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, result, _ = run(
        capsys, "export", work, "--platform", "x", "--destination", tmp_path / "r"
    )

    assert code == 0
    assert result == {"exported": 3}


def test_policy_allows_matching_selection(
    env: dict[str, Path], work: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    x_only_programming(env)
    dest = tmp_path / "ready"

    code, result, _ = run(
        capsys, "export", work, "--platform", "x", "--destination", dest, "--only", "code"
    )

    assert (code, result) == (0, {"exported": 1})
    assert (dest / "x" / "session-01" / "code.mp4").read_bytes() == b"code bytes"


@pytest.mark.parametrize("selection", [None, "code,match", "untagged", "code,untagged"])
def test_policy_denies_batch_before_creating_directories(
    env: dict[str, Path],
    work: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    selection: str | None,
) -> None:
    x_only_programming(env)
    dest = tmp_path / "ready"
    only = ["--only", selection] if selection else []

    code, _, err = run(capsys, "export", work, "--platform", "x", "--destination", dest, *only)

    assert code == 2
    assert "Topic policy for x allows only programming" in err
    assert not dest.exists()
    assert not env["data"].exists()


def test_default_destination_is_not_created_when_denied(
    env: dict[str, Path], work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    x_only_programming(env)

    code, _, _ = run(capsys, "export", work, "--platform", "x", "--only", "match")

    assert code == 2
    assert not env["data"].exists()


@pytest.mark.parametrize("platform", ["tiktok", "instagram", "youtube"])
def test_x_policy_does_not_restrict_other_exports(
    env: dict[str, Path],
    work: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    x_only_programming(env)

    code, result, _ = run(
        capsys, "export", work, "--platform", platform, "--destination", tmp_path / "r"
    )

    assert (code, result) == (0, {"exported": 3})


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", POLICY_FILE),
        ({"platforms": {"twitter": {"allowed_topics": ["programming"]}}}, "Unknown platform"),
        ({"platforms": []}, "keyed by platform"),
    ],
)
def test_malformed_policy_blocks_every_export(
    env: dict[str, Path],
    work: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: object,
    message: str,
) -> None:
    set_policy(env, content)
    dest = tmp_path / "ready"

    code, _, err = run(capsys, "export", work, "--platform", "tiktok", "--destination", dest)

    assert code == 2
    assert message in err
    assert "Traceback" not in err
    assert not dest.exists()


def test_malformed_row_topic_is_reported(
    env: dict[str, Path], work: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    x_only_programming(env)
    rows = queue.load(work)
    rows[0]["topic"] = ["programming"]
    queue.save(work, rows)

    code, _, err = run(
        capsys, "export", work, "--platform", "x", "--destination", tmp_path / "r", "--only", "code"
    )

    assert code == 2
    assert "topic of code.mp4" in err


# Publish


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"client": [], "upload": []}

    def client(settings: Any, channel: str, **kwargs: Any) -> str:
        calls["client"].append(channel)
        return "fake-api"

    def upload(work: Path, api: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["upload"].append((api, kwargs))
        return []

    monkeypatch.setattr(cli.youtube, "client", client)
    monkeypatch.setattr(cli.youtube, "upload", upload)
    return calls


def publish(capsys: pytest.CaptureFixture[str], work: Path, *extra: str) -> tuple[int, Any, str]:
    return run(capsys, "publish", work, "--channel", "main", "--channel-id", CHANNEL, *extra)


def youtube_only_programming(env: dict[str, Path]) -> None:
    set_policy(env, {"platforms": {"youtube": {"allowed_topics": ["programming"]}}})


@pytest.mark.parametrize("selection", [[], ["--only", "match"], ["--only", "code,untagged"]])
def test_publish_denied_before_authentication(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
    selection: list[str],
) -> None:
    youtube_only_programming(env)
    before = (work / "out" / "queue.json").read_bytes()

    code, _, err = publish(capsys, work, *selection, "--execute")

    assert code == 2
    assert "Topic policy for youtube" in err
    assert remote == {"client": [], "upload": []}
    assert (work / "out" / "queue.json").read_bytes() == before


def test_publish_allowed_selection_reaches_client(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    youtube_only_programming(env)

    code, _, _ = publish(capsys, work, "--only", "code")

    assert code == 0
    assert remote["client"] == ["main"]
    assert remote["upload"][0][1]["only"] == {"code"}


def test_publish_ignores_already_uploaded_rows(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    youtube_only_programming(env)
    rows = queue.load(work)
    for row in rows:
        if row["file"] != "code.mp4":
            row.update(youtube_id=f"id-{row['file']}", upload_state="uploaded")
    queue.save(work, rows)

    code, _, _ = publish(capsys, work)

    assert code == 0
    assert remote["client"] == ["main"]


def test_publish_without_policy_is_unrestricted(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, _ = publish(capsys, work)

    assert code == 0
    assert remote["client"] == ["main"]


def test_x_policy_does_not_restrict_youtube(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    x_only_programming(env)

    assert publish(capsys, work)[0] == 0


def test_malformed_policy_blocks_publish_before_authentication(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_policy(env, {"platforms": {"youtube": {"allowed_topics": "programming"}}})

    code, _, err = publish(capsys, work)

    assert code == 2
    assert "allowed_topics for youtube" in err
    assert remote["client"] == []


def test_unknown_selector_is_reported_before_authentication(
    env: dict[str, Path],
    work: Path,
    remote: dict[str, list[Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    youtube_only_programming(env)

    code, _, err = publish(capsys, work, "--only", "nope")

    assert code == 2
    assert "Unknown clip names" in err
    assert remote["client"] == []
