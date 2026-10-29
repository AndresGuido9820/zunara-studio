from pathlib import Path

import pytest

from zunara.config import Settings, safe_name


@pytest.mark.parametrize("name", ["a", "Main_Channel", "clip-01", "9", "x" * 80])
def test_safe_name_accepts_plain_names(name: str) -> None:
    assert safe_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../tokens",
        "a/b",
        "a\\b",
        ".hidden",
        "-flag",
        "_private",
        "with space",
        "dot.json",
        "x" * 81,
        "trailing\n",
        "ñandú",
        "a\x00b",
    ],
)
def test_safe_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        safe_name(name)


def test_from_env_uses_overrides_and_expands_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", "~/cfg")
    monkeypatch.setenv("ZUNARA_DATA_DIR", "~/data")
    monkeypatch.delenv("ZUNARA_YOUTUBE_CLIENT_SECRET", raising=False)

    settings = Settings.from_env()

    assert settings.config_dir == tmp_path / "cfg"
    assert settings.data_dir == tmp_path / "data"
    assert settings.client_secret == tmp_path / "cfg" / "client_secret.json"


def test_from_env_client_secret_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ZUNARA_YOUTUBE_CLIENT_SECRET", str(tmp_path / "elsewhere.json"))

    assert Settings.from_env().client_secret == tmp_path / "elsewhere.json"


def test_from_env_does_not_create_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("ZUNARA_DATA_DIR", str(tmp_path / "data"))

    settings = Settings.from_env()
    settings.token_path("main")

    assert list(tmp_path.iterdir()) == []


def test_token_path_stays_in_tokens_dir(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "cfg", tmp_path / "data", tmp_path / "secret.json")

    assert settings.token_path("main") == tmp_path / "cfg" / "tokens" / "main.json"


@pytest.mark.parametrize("channel", ["../../evil", "/etc/passwd", "a/../b", ".."])
def test_token_path_rejects_traversal(tmp_path: Path, channel: str) -> None:
    settings = Settings(tmp_path / "cfg", tmp_path / "data", tmp_path / "secret.json")

    with pytest.raises(ValueError):
        settings.token_path(channel)
