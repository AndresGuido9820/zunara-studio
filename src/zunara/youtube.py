"""YouTube adapter. Remote writes require explicit execution and an exact channel ID."""

import json
import math
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zunara import queue
from zunara.config import Settings
from zunara.media import probe
from zunara.storage import write_json

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def client(settings: Settings, channel: str, *, interactive: bool = False) -> Any:
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as error:
        raise ValueError(
            "Install the YouTube extra from the checkout: uv sync --locked --no-dev --extra youtube "
            "(or pip install '.[youtube]')."
        ) from error

    token = settings.token_path(channel)
    if token.is_symlink() or token.parent.is_symlink():
        raise ValueError("Token files and their directory must not be symlinks.")
    credentials = None
    if token.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token), SCOPES)
        except (ValueError, OSError):
            if not interactive:
                raise ValueError(
                    f"Authorize this channel first: zunara auth --channel {channel}"
                ) from None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            credentials = None
    if not credentials or not credentials.valid:
        if not interactive:
            raise ValueError(f"Authorize this channel first: zunara auth --channel {channel}")
        if not settings.client_secret.is_file():
            raise ValueError("Missing desktop OAuth client JSON; set ZUNARA_YOUTUBE_CLIENT_SECRET.")
        flow = InstalledAppFlow.from_client_secrets_file(str(settings.client_secret), SCOPES)
        credentials = flow.run_local_server(host="localhost", port=0, open_browser=True)
    write_json(token, json.loads(credentials.to_json()), private=True)
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def identity(api: Any) -> dict[str, str]:
    items = api.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise ValueError("This Google account has no YouTube channel. Create one first.")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def schedule_start(value: str | None, every_hours: float) -> datetime | None:
    if not math.isfinite(every_hours) or every_hours <= 0:
        raise ValueError("--every-hours must be finite and greater than zero.")
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Schedule needs a timezone, e.g. 2030-01-01T12:00:00-05:00.")
    if moment <= datetime.now(UTC):
        raise ValueError("Schedule must be in the future.")
    return moment


@queue.locked
def upload(
    work: Path,
    api: Any,
    *,
    channel_id: str,
    only: set[str] | None = None,
    privacy: str = "private",
    schedule: str | None = None,
    every_hours: float = 3,
    execute: bool = False,
    media_factory: Any = None,
) -> list[dict[str, Any]]:
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("Invalid privacy mode.")
    slot = schedule_start(schedule, every_hours)
    account = identity(api)
    if not channel_id or account["id"] != channel_id:
        raise ValueError("Authenticated channel does not match --channel-id; nothing uploaded.")
    rows = queue.load(work)
    selected = [r for r in queue.select(rows, only) if not r.get("youtube_id")]
    plan = []
    for row in selected:
        if row.get("upload_state") == "uploading":
            raise ValueError(
                "An earlier upload has an unknown outcome. Reconcile in Studio before retrying."
            )
        path = queue.verify_approved(work, row)
        info = probe(path)
        if not info["has_audio"] or info["duration_s"] <= 0:
            raise ValueError(f"Clip must contain video and audio: {path.name}")
        plan.append(
            {
                "file": path.name,
                "channel_id": channel_id,
                "privacy": "private" if slot else privacy,
                "scheduled_at": slot.isoformat() if slot else None,
            }
        )
        if slot:
            slot += timedelta(hours=every_hours)
    if not execute:
        return plan
    if media_factory is None:
        from googleapiclient.http import MediaFileUpload

        media_factory = MediaFileUpload
    for row, planned in zip(selected, plan, strict=True):
        status = {"privacyStatus": planned["privacy"], "selfDeclaredMadeForKids": False}
        if planned["scheduled_at"]:
            status["publishAt"] = planned["scheduled_at"]
        body = {
            "snippet": {
                "title": row["title"],
                "description": row.get("description", ""),
                "tags": row.get("tags", []),
                "categoryId": "24",
                "defaultLanguage": "es",
            },
            "status": status,
        }
        with tempfile.TemporaryDirectory(prefix="zunara-upload-") as folder:
            snapshot = queue.copy_approved(work, row, Path(folder) / row["file"])
            media = media_factory(str(snapshot), chunksize=8 * 1024 * 1024, resumable=True)
            try:
                # Never retry an ambiguous remote write automatically.
                row.update(upload_state="uploading", channel_id=channel_id)
                queue.save(work, rows)
                request = api.videos().insert(part="snippet,status", body=body, media_body=media)
                response = None
                while response is None:
                    _, response = request.next_chunk()
            finally:
                stream = getattr(media, "stream", None)
                if callable(stream):
                    stream().close()
        row.update(
            youtube_id=response["id"],
            upload_state="uploaded",
            uploaded_at=datetime.now(UTC).isoformat(),
            scheduled_at=planned["scheduled_at"],
        )
        queue.save(work, rows)
    return plan
