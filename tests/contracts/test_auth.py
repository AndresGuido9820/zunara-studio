"""OAuth client contract with fake Google modules. No account, network or real token is used."""

import json
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from zunara import youtube
from zunara.config import Settings

pytestmark = pytest.mark.contract

UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"


class RefreshError(Exception):
    pass


class FakeRequest:
    pass


class FakeCredentials:
    """Credentials whose state comes from a synthetic token file."""

    def __init__(self, info: dict[str, Any], google: "FakeGoogle") -> None:
        self.info = dict(info)
        self.google = google
        self.valid = bool(info.get("valid"))
        self.expired = bool(info.get("expired"))
        self.refresh_token = info.get("refresh_token")

    def refresh(self, request: Any) -> None:
        self.google.refreshes.append(request)
        if self.google.refresh_error is not None:
            raise self.google.refresh_error
        self.info.update(token="refreshed-access", valid=True, expired=False)
        self.valid, self.expired = True, False

    def to_json(self) -> str:
        return json.dumps(self.info)


class FakeGoogle:
    """Installs fake ``google``/``googleapiclient`` modules and records how they are used."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.loaded: list[tuple[str, list[str]]] = []
        self.refreshes: list[Any] = []
        self.refresh_error: BaseException | None = None
        self.flows: list[tuple[str, list[str]]] = []
        self.server_kwargs: list[dict[str, Any]] = []
        self.flow_error: BaseException | None = None
        self.granted = {
            "token": "granted-access",
            "refresh_token": "granted-refresh",
            "valid": True,
        }
        self.builds: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        google = self

        class Credentials(FakeCredentials):
            @classmethod
            def from_authorized_user_file(cls, filename: str, scopes: list[str]) -> FakeCredentials:
                google.loaded.append((filename, list(scopes)))
                return cls(json.loads(Path(filename).read_text(encoding="utf-8")), google)

        class Flow:
            @classmethod
            def from_client_secrets_file(cls, filename: str, scopes: list[str]) -> "Flow":
                google.flows.append((filename, list(scopes)))
                return cls()

            def run_local_server(self, **kwargs: Any) -> FakeCredentials:
                google.server_kwargs.append(kwargs)
                if google.flow_error is not None:
                    raise google.flow_error
                return Credentials(google.granted, google)

        def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
            google.builds.append((args, kwargs))
            return {"api": "youtube"}

        modules = {
            "google": {},
            "google.auth": {},
            "google.auth.exceptions": {"RefreshError": RefreshError},
            "google.auth.transport": {},
            "google.auth.transport.requests": {"Request": FakeRequest},
            "google.oauth2": {},
            "google.oauth2.credentials": {"Credentials": Credentials},
            "google_auth_oauthlib": {},
            "google_auth_oauthlib.flow": {"InstalledAppFlow": Flow},
            "googleapiclient": {},
            "googleapiclient.discovery": {"build": build},
        }
        for name, attributes in modules.items():
            module = types.ModuleType(name)
            module.__path__ = []  # type: ignore[attr-defined]
            for key, value in attributes.items():
                setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    return FakeGoogle(monkeypatch)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(tmp_path / "config", tmp_path / "data", tmp_path / "config" / "client.json")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def store_token(settings: Settings, **info: Any) -> Path:
    token = settings.token_path("main")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(json.dumps(info), encoding="utf-8")
    return token


def add_client_secret(settings: Settings) -> Path:
    settings.client_secret.parent.mkdir(parents=True, exist_ok=True)
    settings.client_secret.write_text('{"installed": {"client_id": "synthetic"}}', encoding="utf-8")
    return settings.client_secret


def read_token(settings: Settings) -> dict[str, Any]:
    return json.loads(settings.token_path("main").read_text(encoding="utf-8"))


# Loading


def test_missing_extra_is_reported(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", None)

    with pytest.raises(ValueError, match="YouTube extra"):
        youtube.client(settings, "main")


def test_valid_token_builds_client_without_prompt(google: FakeGoogle, settings: Settings) -> None:
    token = store_token(settings, token="stored-access", refresh_token="stored-refresh", valid=True)

    api = youtube.client(settings, "main")

    assert api == {"api": "youtube"}
    assert google.loaded == [(str(token), [UPLOAD, READONLY])]
    assert google.refreshes == []
    assert google.flows == []
    [(args, kwargs)] = google.builds
    assert args == ("youtube", "v3")
    assert kwargs["cache_discovery"] is False
    assert kwargs["credentials"].info["token"] == "stored-access"


def test_requested_scopes_are_minimal() -> None:
    assert sorted(youtube.SCOPES) == [READONLY, UPLOAD]


def test_valid_token_is_kept_private(google: FakeGoogle, settings: Settings) -> None:
    token = store_token(settings, token="stored-access", refresh_token="stored-refresh", valid=True)
    token.chmod(0o644)
    token.parent.chmod(0o755)

    youtube.client(settings, "main")

    assert mode(token) == 0o600
    assert mode(token.parent) == 0o700


# Refresh


def test_expired_token_is_refreshed_and_saved(google: FakeGoogle, settings: Settings) -> None:
    token = store_token(
        settings, token="old-access", refresh_token="stored-refresh", valid=False, expired=True
    )

    youtube.client(settings, "main")

    assert len(google.refreshes) == 1
    assert isinstance(google.refreshes[0], FakeRequest)
    assert google.flows == []
    saved = read_token(settings)
    assert saved["token"] == "refreshed-access"
    assert saved["refresh_token"] == "stored-refresh"
    assert mode(token) == 0o600
    assert sorted(p.name for p in token.parent.iterdir()) == ["main.json"]


def test_expired_token_without_refresh_token_is_not_refreshed(
    google: FakeGoogle, settings: Settings
) -> None:
    store_token(settings, token="old-access", valid=False, expired=True)

    with pytest.raises(ValueError, match="Authorize this channel first"):
        youtube.client(settings, "main")
    assert google.refreshes == []


# Non-interactive refusal


def test_missing_token_is_refused_without_side_effects(
    google: FakeGoogle, settings: Settings
) -> None:
    add_client_secret(settings)

    with pytest.raises(ValueError, match="zunara auth --channel main"):
        youtube.client(settings, "main")

    assert google.loaded == []
    assert google.flows == []
    assert google.builds == []
    assert not (settings.config_dir / "tokens").exists()


@pytest.mark.parametrize(
    "info",
    [
        {"token": "x", "valid": False},
        {"token": "x", "valid": False, "expired": False, "refresh_token": "r"},
    ],
)
def test_invalid_token_is_refused_and_left_untouched(
    google: FakeGoogle, settings: Settings, info: dict[str, Any]
) -> None:
    add_client_secret(settings)
    token = store_token(settings, **info)
    before = token.read_bytes()

    with pytest.raises(ValueError, match="Authorize this channel first"):
        youtube.client(settings, "main")

    assert token.read_bytes() == before
    assert google.flows == []
    assert google.builds == []


@pytest.mark.parametrize("channel", ["../main", "main/../../x", "", ".main", "a b"])
def test_unsafe_channel_alias_reads_nothing(
    google: FakeGoogle, settings: Settings, channel: str
) -> None:
    with pytest.raises(ValueError, match="names"):
        youtube.client(settings, channel, interactive=True)
    assert google.loaded == []
    assert google.flows == []


# Interactive loopback OAuth


def test_interactive_requires_client_secret(google: FakeGoogle, settings: Settings) -> None:
    with pytest.raises(ValueError, match="ZUNARA_YOUTUBE_CLIENT_SECRET"):
        youtube.client(settings, "main", interactive=True)
    assert google.flows == []
    assert not settings.token_path("main").exists()


def test_interactive_runs_loopback_flow_and_saves_private_token(
    google: FakeGoogle, settings: Settings
) -> None:
    secret = add_client_secret(settings)

    api = youtube.client(settings, "main", interactive=True)

    assert api == {"api": "youtube"}
    assert google.flows == [(str(secret), [UPLOAD, READONLY])]
    [server] = google.server_kwargs
    assert server["host"] in {"localhost", "127.0.0.1"}
    assert server["port"] == 0
    token = settings.token_path("main")
    assert read_token(settings) == google.granted
    assert mode(token) == 0o600
    assert mode(token.parent) == 0o700
    assert google.builds[0][1]["credentials"].info == google.granted


def test_interactive_reuses_valid_token(google: FakeGoogle, settings: Settings) -> None:
    add_client_secret(settings)
    store_token(settings, token="stored-access", refresh_token="stored-refresh", valid=True)

    youtube.client(settings, "main", interactive=True)

    assert google.flows == []


def test_interactive_replaces_invalid_token(google: FakeGoogle, settings: Settings) -> None:
    add_client_secret(settings)
    store_token(settings, token="stale", valid=False)

    youtube.client(settings, "main", interactive=True)

    assert len(google.flows) == 1
    assert read_token(settings) == google.granted


def test_token_is_not_written_through_symlinked_directory(
    google: FakeGoogle, settings: Settings, tmp_path: Path
) -> None:
    add_client_secret(settings)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (settings.config_dir / "tokens").symlink_to(elsewhere)

    with pytest.raises(ValueError, match="symlink"):
        youtube.client(settings, "main", interactive=True)
    assert list(elsewhere.iterdir()) == []


def test_flow_failure_keeps_existing_token(google: FakeGoogle, settings: Settings) -> None:
    add_client_secret(settings)
    token = store_token(settings, token="stale", valid=False)
    before = token.read_bytes()
    google.flow_error = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        youtube.client(settings, "main", interactive=True)
    assert token.read_bytes() == before
    assert sorted(p.name for p in token.parent.iterdir()) == ["main.json"]


# Revoked refresh tokens


def test_revoked_refresh_token_asks_to_authorize(google: FakeGoogle, settings: Settings) -> None:
    store_token(settings, token="old", refresh_token="revoked", valid=False, expired=True)
    google.refresh_error = RefreshError("invalid_grant")

    with pytest.raises(ValueError, match="Authorize this channel first"):
        youtube.client(settings, "main")


def test_interactive_auth_recovers_from_revoked_refresh_token(
    google: FakeGoogle, settings: Settings
) -> None:
    add_client_secret(settings)
    store_token(settings, token="old", refresh_token="revoked", valid=False, expired=True)
    google.refresh_error = RefreshError("invalid_grant")

    youtube.client(settings, "main", interactive=True)

    assert len(google.flows) == 1
    assert read_token(settings) == google.granted


def test_interactive_auth_recovers_corrupt_token(google: FakeGoogle, settings: Settings) -> None:
    add_client_secret(settings)
    token = store_token(settings, valid=False)
    token.write_text("not JSON", encoding="utf-8")
    youtube.client(settings, "main", interactive=True)
    assert len(google.flows) == 1
    assert read_token(settings) == google.granted


def test_corrupt_token_noninteractive_requests_auth_without_overwriting(
    google: FakeGoogle, settings: Settings
) -> None:
    token = store_token(settings, valid=False)
    token.write_text("not JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="Authorize this channel first"):
        youtube.client(settings, "main")
    assert token.read_text() == "not JSON"
    assert google.flows == []
