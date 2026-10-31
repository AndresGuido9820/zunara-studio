"""Native Instagram Reels publisher: Graph API (Facebook Login) with a resumable local upload.

The flow has separate, resumable phases and never polls in a loop:

1. ``publish`` creates a REELS container and streams an approved snapshot to the fixed upload
   host. The container then processes on Meta's side; a processing container is not a post.
2. ``status --refresh`` reads container status (GET only) and never uploads or publishes.
3. ``finish`` publishes only containers already reported ``FINISHED`` for the same account and
   approved bytes.

Every phase is recorded in ``out/instagram-publications.json`` before the request that could
change remote state, so an interrupted run never creates, uploads or publishes twice.
"""

import contextlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from zunara import queue, social
from zunara.media import probe
from zunara.storage import write_json

GRAPH = "https://graph.facebook.com"
UPLOAD = "https://rupload.facebook.com/ig-api-upload"
API_VERSION = "v26.0"
# Names of the token sources, not secrets.
TOKEN_ENV = "ZUNARA_INSTAGRAM_ACCESS_TOKEN"  # nosec B105
TOKEN_FILE = "instagram-access-token"  # nosec B105
LEDGER = "instagram-publications.json"
LEDGER_VERSION = 1
CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 30
MIN_SECONDS = 3.0
MAX_SECONDS = 900.0
# Zunara's conservative local cap, not Meta's limit (Meta's Postman collection says 1 GB).
MAX_BYTES = 300 * 1024 * 1024
VIDEO_CODECS = {"h264", "hevc"}
AUDIO_CODECS = {"aac"}
AUDIO_SAMPLE_RATE = 48000  # Meta's official Reels collection specifies 48 kHz AAC.
TIMEOUT = (10, 60)
UPLOAD_TIMEOUT = (10, 900)
NUMERIC_ID = re.compile(r"[0-9]{1,32}")
USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
VERSION = re.compile(r"v[0-9]{1,3}\.[0-9]")
SHA256 = re.compile(r"[0-9a-f]{64}")
CONTAINER_CODES = {"IN_PROGRESS", "FINISHED", "ERROR", "EXPIRED", "PUBLISHED"}
STATES = (
    "creating",  # Create POST about to be sent; no container ID known.
    "unknown",  # Create POST sent but its outcome is unknown; no container ID known.
    "created",  # Container ID saved; the video bytes have not been sent.
    "uploading",  # Video transfer started; its outcome may be unknown.
    "processing",  # Bytes accepted; Meta is processing the container. Not a post.
    "ready",  # Container reported FINISHED; ``finish --execute`` may publish it.
    "publishing",  # Publish POST about to be sent or its outcome unknown.
    "published",  # Publish confirmed with a media ID.
    "api_published",  # Container reported PUBLISHED, but no media ID was received.
    "failed",  # Definite rejection or container ERROR; never retried automatically.
    "expired",  # Container EXPIRED before publishing.
)
REFRESHABLE = {"created", "uploading", "processing", "ready", "publishing"}
CHECKABLE = REFRESHABLE - {"publishing"}  # A lost publish response is never retried.
HTTP_ERRORS = {
    400: "Instagram API rejected the request parameters",
    401: f"Instagram API rejected the access token; check {TOKEN_ENV} or the token file",
    403: "The token lacks permission for this Instagram account",
    404: "Instagram account or container not found",
    429: "Instagram API rate limit reached",
}


class Unknown(Exception):
    """The request may or may not have changed remote state."""


class Rejected(ValueError):
    """The API definitely rejected the request; nothing was accepted."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def token_status(config_dir: Path) -> dict[str, Any]:
    """Describe where a token would come from without reading it."""
    path = config_dir / TOKEN_FILE
    source = "environment" if os.environ.get(TOKEN_ENV, "").strip() else None
    private = None
    if path.is_symlink():
        private, source = False, source or "file"
    elif path.exists():
        private, source = not stat.S_IMODE(path.stat().st_mode) & 0o077, source or "file"
    return {"token_source": source, "token_file": str(path), "token_file_private": private}


def load_token(config_dir: Path) -> str:
    """Return the access token from the environment or a private, non-symlink file."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        path = config_dir / TOKEN_FILE
        if path.is_symlink():
            raise ValueError("The Instagram token file must not be a symlink.")
        if not path.is_file():
            raise ValueError(f"No Instagram access token; set {TOKEN_ENV} or create {path}.")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f"The Instagram token file must be private: chmod 600 {path}")
        token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[\x21-\x7e]{16,4096}", token):
        raise ValueError("The Instagram access token is malformed.")
    return token


def check_version(version: str) -> str:
    if not VERSION.fullmatch(version):
        raise ValueError("--api-version must look like v26.0.")
    return version


def check_account(account_id: str, username: str) -> tuple[str, str]:
    handle = username.removeprefix("@")
    if not NUMERIC_ID.fullmatch(account_id):
        raise ValueError("--account-id must be the numeric Instagram professional account ID.")
    if not USERNAME.fullmatch(handle):
        raise ValueError("--username must be an Instagram username.")
    return account_id, handle.lower()


def _error(response: requests.Response) -> str:
    message = HTTP_ERRORS.get(response.status_code, "Instagram API rejected the request")
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    # Only the numeric Graph error code is kept; messages may echo request data.
    suffix = f", error {code}" if isinstance(code, int) and not isinstance(code, bool) else ""
    return f"{message} (HTTP {response.status_code}{suffix})."


class Client:
    """Graph API client with fixed hosts. No redirects, retries, netrc or proxy overrides."""

    def __init__(self, token: str, session: requests.Session | None = None) -> None:
        self._token = token
        if session is None:
            session = requests.Session()
            session.trust_env = False
        self.session = session

    def _send(self, method: str, url: str, *, timeout: Any, **kwargs: Any) -> Any:
        try:
            response = self.session.request(
                method, url, allow_redirects=False, timeout=timeout, **kwargs
            )
        except requests.RequestException:
            raise Unknown("Instagram API did not answer.") from None
        if 400 <= response.status_code < 500:
            raise Rejected(_error(response))
        if response.status_code != 200:
            raise Unknown(f"Unexpected Instagram API response (HTTP {response.status_code}).")
        try:
            data = response.json()
        except ValueError:
            raise Unknown("Instagram API returned a response that is not JSON.") from None
        if not isinstance(data, dict):
            raise Unknown("Instagram API returned an unexpected response shape.")
        return data

    def graph(
        self, method: str, version: str, node: str, edge: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        if not NUMERIC_ID.fullmatch(node) or edge not in {"", "media", "media_publish"}:
            raise ValueError("Refusing to call an unexpected Instagram API path.")
        url = f"{GRAPH}/{check_version(version)}/{node}" + (f"/{edge}" if edge else "")
        headers = {"Authorization": f"Bearer {self._token}"}
        return self._send(method, url, timeout=TIMEOUT, headers=headers, **kwargs)

    def read(self, version: str, node: str, fields: str) -> dict[str, Any]:
        """GET a node; any failure is reported as a plain ValueError."""
        try:
            return self.graph("GET", version, node, params={"fields": fields})
        except Unknown as error:
            raise ValueError(str(error)) from None

    def upload(self, version: str, container_id: str, snapshot: Path) -> dict[str, Any]:
        """Stream the snapshot to the fixed upload host; a returned ``uri`` is never used."""
        if not NUMERIC_ID.fullmatch(container_id):
            raise ValueError("Refusing to upload to an unexpected container ID.")
        size = snapshot.stat().st_size
        headers = {
            "Authorization": f"OAuth {self._token}",
            "offset": "0",
            "file_size": str(size),
        }
        url = f"{UPLOAD}/{check_version(version)}/{container_id}"
        with snapshot.open("rb") as body:
            return self._send("POST", url, timeout=UPLOAD_TIMEOUT, headers=headers, data=body)


def identity(client: Client, version: str, account_id: str, username: str) -> dict[str, str]:
    """Fetch the account and require both the exact ID and username."""
    data = client.read(version, account_id, "id,username")
    found_id, found_name = data.get("id"), data.get("username")
    if found_id != account_id:
        raise ValueError("The token's Instagram account ID does not match --account-id.")
    if not isinstance(found_name, str) or found_name.lower() != username:
        raise ValueError("The Instagram account username does not match --username.")
    return {"id": found_id, "username": found_name}


def caption(row: dict[str, Any]) -> str:
    text = row.get("description", "").strip() or row["title"].strip()
    if len(text) > CAPTION_LIMIT:
        raise ValueError(f"Instagram caption exceeds {CAPTION_LIMIT} characters: {row['file']}")
    if len(re.findall(r"(?<!\w)#\w", text)) > HASHTAG_LIMIT:
        raise ValueError(f"Instagram caption has more than {HASHTAG_LIMIT} hashtags: {row['file']}")
    return text


def check_media(path: Path, name: str) -> dict[str, Any]:
    """Reject media the Reels endpoint is known not to accept, before any network call."""
    size = path.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise ValueError(f"Clip exceeds Zunara's local {MAX_BYTES}-byte Instagram cap: {name}")
    info = probe(path)
    if info["codec"] not in VIDEO_CODECS:
        raise ValueError(f"Instagram Reels need H.264 or HEVC video: {name}")
    if info.get("has_audio") and (
        info.get("audio_codec") not in AUDIO_CODECS
        or info.get("audio_sample_rate") != AUDIO_SAMPLE_RATE
    ):
        raise ValueError(f"Instagram Reels need {AUDIO_SAMPLE_RATE} Hz AAC audio: {name}")
    if not MIN_SECONDS <= info["duration_s"] <= MAX_SECONDS:
        raise ValueError(
            f"Instagram Reels must last {MIN_SECONDS:g}–{MAX_SECONDS:g} seconds: {name}"
        )
    return {"bytes": size, **info}


def load_ledger(work: Path) -> dict[str, Any]:
    """Load the per-workspace Instagram receipts; anything unexpected fails closed."""
    path = work / "out" / LEDGER
    if path.is_symlink():
        raise ValueError(f"{LEDGER} must not be a symlink.")
    if not path.exists():
        return {"version": LEDGER_VERSION, "records": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        raise ValueError(f"{LEDGER} has an unknown format or version.")
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError(f"{LEDGER} must contain a records array.")
    keys = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("state") not in STATES
            or not NUMERIC_ID.fullmatch(str(record.get("account_id")))
            or not SHA256.fullmatch(str(record.get("approved_sha256")))
            or not VERSION.fullmatch(str(record.get("api_version")))
            or not isinstance(record.get("fields", {}), dict)
            or (
                record.get("container_id") is not None
                and not NUMERIC_ID.fullmatch(str(record["container_id"]))
            )
        ):
            raise ValueError(f"{LEDGER} contains an invalid record.")
        key = (record["account_id"], record["approved_sha256"])
        if key in keys:
            raise ValueError(f"{LEDGER} contains a duplicate record.")
        keys.add(key)
    return data


def _save(work: Path, ledger: dict[str, Any]) -> None:
    # write_json stages with mkstemp, so the replaced file is owner-only (0600).
    write_json(work / "out" / LEDGER, ledger)


def _index(ledger: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["account_id"], r["approved_sha256"]): r for r in ledger["records"]}


def _earlier(
    work: Path, rows: list[dict[str, Any]], row: dict[str, Any], username: str
) -> str | None:
    """Outcomes recorded by browser work (publications.json) or the Upload-Post adapter."""
    prior = social._prior_outcome(social._prior_entries(work), row, "instagram", len(rows) == 1)
    if prior:
        return f"publications.json records {prior}"
    for record in social.load_ledger(work)["records"]:
        if (
            record["platform"] == "instagram"
            and record["approved_sha256"] == row["approved_sha256"]
            and str(record.get("account", "")).lower() == username
            and record["state"] != "failed"
        ):
            return f"{social.LEDGER} records {record['state']}"
    return None


def next_commands(work: Path, account_id: str, username: str, stems: list[str]) -> list[str]:
    only = ",".join(stems)
    return [
        f"zunara instagram status {work} --refresh",
        f"zunara instagram finish {work} --only {only} --account-id {account_id} "
        f"--username {username} --execute",
    ]


def plan(
    work: Path, only: set[str], account_id: str, username: str, ai_generated: bool
) -> list[dict[str, Any]]:
    """Offline preflight of the whole selection. The caller holds the workspace lock."""
    receipts = _index(load_ledger(work))
    rows = queue.load(work)
    chosen = queue.select(rows, only)
    for row in chosen:
        queue.verify_approved(work, row)
        caption(row)
    items = []
    for row in chosen:
        item: dict[str, Any] = {"file": row["file"], "account_id": account_id}
        stored = receipts.get((account_id, row["approved_sha256"]))
        earlier = None if stored else _earlier(work, rows, row, username)
        if stored:
            item.update(action="skip", reason=f"recorded in {LEDGER}: {stored['state']}")
        elif earlier:
            item.update(action="skip", reason=earlier)
        else:
            media = check_media(queue.verify_approved(work, row), row["file"])
            item.update(
                action="create",
                fields=_fields(row, ai_generated),
                media={k: media[k] for k in ("bytes", "duration_s", "width", "height", "codec")},
            )
        items.append({**item, "_row": row})
    return items


def _fields(row: dict[str, Any], ai_generated: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption(row),
        "share_to_feed": True,
    }
    if ai_generated:
        fields["is_ai_generated"] = True
    return fields


def _public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in i.items() if k != "_row"} for i in items]


def _create_and_upload(
    work: Path, client: Client, ledger: dict[str, Any], item: dict[str, Any], **context: Any
) -> dict[str, Any]:
    row, version = item["_row"], context["version"]
    with tempfile.TemporaryDirectory(prefix="zunara-instagram-") as folder:
        # The snapshot is re-verified against the approved hash; only it is ever sent.
        snapshot = queue.copy_approved(work, row, Path(folder) / row["file"])
        if snapshot.stat().st_size != item["media"]["bytes"]:
            raise ValueError(f"Media changed after preflight: {row['file']}")
        record: dict[str, Any] = {
            "account_id": context["account_id"],
            "username": context["username"],
            "approved_sha256": row["approved_sha256"],
            "file": row["file"],
            "api_version": version,
            "state": "creating",
            "container_id": None,
            "media_id": None,
            "permalink": None,
            "status_code": None,
            "ai_generated": "is_ai_generated" in item["fields"],
            "fields": dict(item["fields"]),  # Frozen by Meta at creation.
            "created_at": _now(),
            "updated_at": _now(),
        }
        ledger["records"].append(record)
        _save(work, ledger)  # Saved before the create POST: a crash leaves "creating".

        def mark(state: str, **fields: Any) -> None:
            record.update(state=state, updated_at=_now(), **fields)
            _save(work, ledger)

        try:
            data = client.graph(
                "POST", version, context["account_id"], "media", json=item["fields"]
            )
        except Rejected as error:
            mark("failed", phase="create")
            raise ValueError(f"{error} Recorded for {row['file']}; not retried.") from None
        except Unknown:
            mark("unknown")
            raise ValueError(
                f"Container creation outcome unknown for {row['file']}; it will not be "
                "retried. Check the account before deciding what to do."
            ) from None
        container_id = data.get("id")
        if not isinstance(container_id, str) or not NUMERIC_ID.fullmatch(container_id):
            mark("unknown")
            raise ValueError(f"Instagram returned no usable container ID for {row['file']}.")
        mark("created", container_id=container_id)
        mark("uploading")  # Saved before the transfer: a crash leaves "uploading".
        try:
            reply = client.upload(version, container_id, snapshot)
        except Rejected as error:
            mark("failed", phase="upload")
            raise ValueError(f"{error} Recorded for {row['file']}; not retried.") from None
        except Unknown:
            reply = {}
        if reply.get("success") is not True:
            raise ValueError(
                f"Upload outcome unknown for {row['file']}; it will not be resent. "
                f"Run 'zunara instagram status {work} --refresh'."
            )
        mark("processing")
    return record


def publish(
    work: Path,
    client: Client,
    items: list[dict[str, Any]],
    *,
    account_id: str,
    username: str,
    version: str,
    execute: bool,
) -> dict[str, Any]:
    """Preview (default) or create and upload containers. The caller holds the lock."""
    found = identity(client, version, account_id, username)
    result: dict[str, Any] = {
        "mode": "execute" if execute else "preview",
        "account": found,
        "api_version": version,
        "items": _public(items),
    }
    if not execute:
        return result
    ledger = load_ledger(work)
    records = [
        _create_and_upload(
            work, client, ledger, item, account_id=account_id, username=username, version=version
        )
        for item in items
        if item["action"] == "create"
    ]
    stems = [Path(r["file"]).stem for r in records]
    return {**result, "records": records, "next": next_commands(work, account_id, username, stems)}


def _safe_permalink(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 200:
        return None
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.netloc not in {"www.instagram.com", "instagram.com"}
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"/(reel|reels|p|tv)/[A-Za-z0-9_-]{1,64}/?", parts.path)
    ):
        return None
    return f"https://{parts.netloc}{parts.path}"


def _apply_code(record: dict[str, Any], code: str) -> None:
    """Move a record forward from a container status code; never backwards."""
    if code == "PUBLISHED":
        state = "published" if record.get("media_id") else "api_published"
    elif code in {"ERROR", "EXPIRED"}:
        state = "failed" if code == "ERROR" else "expired"
    elif record["state"] == "publishing":
        state = "publishing"  # A lost publish response stays unresolved.
    else:
        state = "ready" if code == "FINISHED" else "processing"
    record.update(state=state, status_code=code, updated_at=_now())


def container_code(client: Client, record: dict[str, Any]) -> str:
    data = client.read(record["api_version"], record["container_id"], "status_code,status")
    code = data.get("status_code")
    if code not in CONTAINER_CODES:
        raise ValueError("Unrecognized container status; left unresolved.")
    return str(code)


def _fill_permalink(client: Client, record: dict[str, Any]) -> None:
    data = client.read(record["api_version"], record["media_id"], "id,permalink")
    if data.get("id") == record["media_id"]:
        record.update(permalink=_safe_permalink(data.get("permalink")), updated_at=_now())


def status(work: Path, client: Client | None = None) -> dict[str, Any]:
    """Return receipts; with a client, read container and media status (GET only)."""
    ledger = load_ledger(work)
    errors = []
    if client is not None:
        for record in ledger["records"]:
            try:
                if record["state"] in REFRESHABLE and record.get("container_id"):
                    _apply_code(record, container_code(client, record))
                if record["state"] == "published" and not record.get("permalink"):
                    _fill_permalink(client, record)
            except ValueError as error:
                errors.append({"file": record["file"], "error": str(error)})
        _save(work, ledger)
    return {"records": ledger["records"], "refresh_errors": errors}


def finish_plan(work: Path, only: set[str], account_id: str, username: str) -> list[dict[str, Any]]:
    """Offline checks before any request: approval, account and frozen caption.

    Meta fixes a container's caption at creation, so queue text edited afterwards would not
    apply; such a clip is rejected instead of implying the edit will be published.
    """
    receipts = _index(load_ledger(work))
    items = []
    for row in queue.select(queue.load(work), only):
        queue.verify_approved(work, row)
        record = receipts.get((account_id, row["approved_sha256"]))
        item: dict[str, Any] = {"file": row["file"], "_row": row}
        if record is None:
            item.update(action="none", reason="no Instagram container for these approved bytes")
            items.append(item)
            continue
        if record.get("username") != username:
            raise ValueError(f"The saved receipt for {row['file']} names another username.")
        item.update(state=record["state"], fields=record.get("fields"))
        if record["state"] in CHECKABLE and record.get("container_id"):
            if not isinstance(record.get("fields"), dict):
                raise ValueError(f"The receipt for {row['file']} lacks its creation fields.")
            if caption(row) != record["fields"].get("caption"):
                raise ValueError(
                    f"The caption of {row['file']} changed after its container was created; "
                    "Instagram keeps the caption sent at creation. Restore the saved caption."
                )
            item.update(action="check", container_id=record["container_id"])
        else:
            item.update(action="wait", reason=f"state is {record['state']}")
        items.append(item)
    return items


def finish(
    work: Path,
    client: Client,
    items: list[dict[str, Any]],
    *,
    account_id: str,
    username: str,
    version: str,
    execute: bool,
) -> dict[str, Any]:
    """Read identity and current container status; with ``execute``, publish FINISHED ones.

    The preview sends GET requests only and writes nothing. The caller holds the lock.
    """
    found = identity(client, version, account_id, username)
    ledger = load_ledger(work)
    receipts = _index(ledger)
    done = []
    for item in items:
        if item["action"] != "check":
            continue
        stored = receipts[(account_id, item["_row"]["approved_sha256"])]
        queue.verify_approved(work, item["_row"])  # The source must still match the approval.
        code = container_code(client, stored)
        record = stored if execute else dict(stored)
        _apply_code(record, code)
        ready = record["state"] == "ready"
        item.update(
            container_status=code,
            state=record["state"],
            action="publish" if ready else "wait",
            **({} if ready else {"reason": f"container is {code}"}),
        )
        if not execute:
            continue
        _save(work, ledger)
        if not ready:
            done.append(record)
            continue
        record.update(state="publishing", updated_at=_now())
        _save(work, ledger)  # Saved before the publish POST.
        try:
            data = client.graph(
                "POST",
                record["api_version"],
                account_id,
                "media_publish",
                json={"creation_id": record["container_id"]},
            )
        except Rejected as error:
            record.update(state="failed", phase="publish", updated_at=_now())
            _save(work, ledger)
            raise ValueError(f"{error} Recorded for {record['file']}; not retried.") from None
        except Unknown:
            data = {}
        media_id = data.get("id")
        if not isinstance(media_id, str) or not NUMERIC_ID.fullmatch(media_id):
            raise ValueError(
                f"Publish outcome unknown for {record['file']}; it will not be retried. "
                f"Run 'zunara instagram status {work} --refresh'."
            )
        record.update(state="published", media_id=media_id, updated_at=_now())
        _save(work, ledger)
        # Published either way; 'status --refresh' can fill a missing permalink later.
        with contextlib.suppress(ValueError):
            _fill_permalink(client, record)
        _save(work, ledger)
        done.append(record)
    result = {"mode": "execute" if execute else "preview", "account": found}
    return {**result, "items": _public(items), **({"records": done} if execute else {})}
