"""Upload-Post adapter: publish approved clips to several platforms through one HTTPS API.

Remote writes require explicit execution, an exact profile and an exact connected account for
every platform. A ledger record is written before each upload request and blocks any repeat for
the same profile, platform and approved media hash, whatever the outcome.
"""

import json
import os
import re
import stat
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from zunara import queue
from zunara.storage import write_json

API = "https://api.upload-post.com/api"
KEY_ENV = "UPLOAD_POST_API_KEY"
KEY_FILE = "upload-post-api-key"
LEDGER = "social-publications.json"
LEDGER_VERSION = 1
PLATFORMS = ("tiktok", "instagram", "youtube", "x")
SETUP = {
    "docs": "https://docs.upload-post.com/",
    "openapi": "https://docs.upload-post.com/openapi.json",
    "pricing": "https://www.upload-post.com/pricing/",
}
STATES = ("submitting", "unknown", "accepted", "scheduled", "completed", "failed")
# Plans known to include TikTok (Upload-Post pricing, October 2026). Free excludes TikTok;
# any other or unknown plan name is not assumed to be entitled.
TIKTOK_PLANS = {"basic", "professional", "advanced", "business"}
TIMEOUT = (10, 60)
UPLOAD_TIMEOUT = (10, 600)
X_LIMIT = 280
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}")
LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")
HTTP_ERRORS = {
    400: "Upload-Post rejected the request parameters.",
    401: f"Upload-Post rejected the API key; check {KEY_ENV} or the key file.",
    403: "The Upload-Post plan or permissions do not allow this request.",
    404: "Upload-Post profile or resource not found; check --profile.",
    429: "Upload-Post upload or scheduling limit reached for this plan.",
}


class Unknown(Exception):
    """The request may or may not have been processed remotely."""


class _ApiKey(requests.auth.AuthBase):
    # An explicit auth object also stops requests from applying credentials from ~/.netrc.
    def __init__(self, key: str) -> None:
        self.key = key

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        request.headers["Authorization"] = f"Apikey {self.key}"
        return request


def key_status(config_dir: Path) -> dict[str, Any]:
    """Describe where an API key would come from, without reading it into the result."""
    path = config_dir / KEY_FILE
    source = "environment" if os.environ.get(KEY_ENV, "").strip() else None
    private = None
    if path.is_symlink():
        private, source = False, source or "file"
    elif path.exists():
        private, source = not stat.S_IMODE(path.stat().st_mode) & 0o077, source or "file"
    return {"api_key_source": source, "key_file": str(path), "key_file_private": private}


def load_key(config_dir: Path) -> str:
    """Return the API key from the environment or a private key file."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        path = config_dir / KEY_FILE
        if path.is_symlink():
            raise ValueError("The Upload-Post key file must not be a symlink.")
        if not path.is_file():
            raise ValueError(f"No Upload-Post API key; set {KEY_ENV} or create {path}.")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f"The Upload-Post key file must be private: chmod 600 {path}")
        key = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[\x21-\x7e]{8,512}", key):
        raise ValueError("The Upload-Post API key is malformed.")
    return key


class Client:
    """Minimal Upload-Post client. Redirects are never followed and nothing is retried."""

    def __init__(self, key: str, session: requests.Session | None = None) -> None:
        self.auth = _ApiKey(key)
        self.session = session or requests.Session()

    def call(self, method: str, path: str, *, upload: bool = False, **kwargs: Any) -> Any:
        try:
            response = self.session.request(
                method,
                API + path,
                auth=self.auth,
                allow_redirects=False,
                timeout=UPLOAD_TIMEOUT if upload else TIMEOUT,
                **kwargs,
            )
        except requests.RequestException:
            raise Unknown("Upload-Post did not answer.") from None
        if response.status_code in HTTP_ERRORS:
            raise ValueError(HTTP_ERRORS[response.status_code])
        if response.status_code not in {200, 202}:
            raise Unknown(f"Unexpected Upload-Post response (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError:
            raise Unknown("Upload-Post returned a response that is not JSON.") from None


def _get(client: Client, path: str, **params: str) -> dict[str, Any]:
    try:
        data = client.call("GET", path, params=params or None)
    except Unknown as error:
        raise ValueError(str(error)) from None
    if not isinstance(data, dict):
        raise ValueError("Upload-Post returned an unexpected response shape.")
    return data


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and SAFE_NAME.fullmatch(value) else None


def _plan(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.+-]{0,39}", value) else None


def sanitize_account(value: object) -> dict[str, Any] | None:
    """Keep only identifiers and the reauthorization flag of a connected account."""
    if isinstance(value, str):
        return {"username": _text(value), "handle": None, "reauth_required": None}
    if not isinstance(value, dict):
        return None
    reauth = value.get("reauth_required")
    return {
        "username": _text(value.get("username")),
        "handle": _text(value.get("handle")),
        "reauth_required": reauth if isinstance(reauth, bool) else None,
    }


def sanitize_profile(profile: object) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("Upload-Post returned an unexpected profile.")
    accounts = profile.get("social_accounts") or {}
    return {
        "username": _text(profile.get("username")),
        "accounts": {
            name: sanitize_account(value)
            for name, value in (accounts.items() if isinstance(accounts, dict) else [])
            if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,32}", name)
        },
    }


def accounts(client: Client) -> dict[str, Any]:
    """List profiles with connected account identifiers; other provider data is dropped."""
    data = _get(client, "/uploadposts/users")
    profiles = data.get("profiles")
    return {
        "plan": _plan(data.get("plan")),
        "profiles": [sanitize_profile(p) for p in (profiles if isinstance(profiles, list) else [])],
    }


def parse_schedule(value: str | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Schedule needs a timezone, e.g. 2030-01-01T12:00:00-05:00.")
    if moment <= now or moment > now + timedelta(days=365):
        raise ValueError("Schedule must be in the future and at most 365 days ahead.")
    return moment.astimezone(UTC)


def parse_accounts(values: list[str], platforms: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for value in values:
        platform, sep, ident = value.partition("=")
        if not sep or platform not in PLATFORMS or not SAFE_NAME.fullmatch(ident.lstrip("@")):
            raise ValueError("Use --account PLATFORM=ACCOUNT_ID, e.g. --account tiktok=myhandle.")
        if platform in expected:
            raise ValueError(f"--account names {platform} twice.")
        expected[platform] = ident.lstrip("@")
    if set(expected) != set(platforms):
        raise ValueError("Give exactly one --account for every selected --platform.")
    return expected


def _now() -> str:
    return datetime.now(UTC).isoformat()


def load_ledger(work: Path) -> dict[str, Any]:
    """Load the per-workspace publication ledger; anything unexpected fails closed."""
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
            or record.get("platform") not in PLATFORMS
            or record.get("state") not in STATES
            or not isinstance(record.get("profile"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("approved_sha256")))
        ):
            raise ValueError(f"{LEDGER} contains an invalid record.")
        key = (record["profile"], record["platform"], record["approved_sha256"])
        if key in keys:
            raise ValueError(f"{LEDGER} contains a duplicate record.")
        keys.add(key)
    return data


def _save_ledger(work: Path, ledger: dict[str, Any]) -> None:
    # write_json stages with mkstemp, so the replaced file is owner-only (0600).
    write_json(work / "out" / LEDGER, ledger)


# Earlier records in work/publications.json: states that mean the clip already exists on the
# platform (skip it) and states that mean it was never posted. Anything else fails closed.
PRIOR_DONE = {"published", "scheduled", "uploaded", "public", "private", "unlisted"}
PRIOR_NOT_POSTED = {"exported", "draft", "failed", "cancelled", "canceled", "superseded"}


def _prior_entries(work: Path) -> list[tuple[str, dict[str, Any]]]:
    path = work / "publications.json"
    if path.is_symlink():
        raise ValueError("publications.json must not be a symlink.")
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("publications"), list):
        data = data["publications"]
    if isinstance(data, list):
        pairs = [(e.get("platform"), e) for e in data if isinstance(e, dict)]
        if len(pairs) != len(data):
            raise ValueError("publications.json entries must be objects.")
    elif isinstance(data, dict):
        # Keyed by platform: {"youtube": {"status": "published", "url": ..., "file": ...}}.
        pairs = [(k, v) for k, v in data.items() if k in PLATFORMS]
        if not all(isinstance(v, dict) for _, v in pairs):
            raise ValueError("publications.json platform entries must be objects.")
    else:
        raise ValueError("publications.json must be an object or an array.")
    return [(str(k), v) for k, v in pairs]


def _prior_outcome(
    entries: list[tuple[str, dict[str, Any]]], row: dict[str, Any], platform: str, single: bool
) -> str | None:
    """Return the earlier state recorded for this clip on ``platform``, if it was posted."""
    for name, entry in entries:
        if name != platform:
            continue
        if entry.get("sha256") is not None:
            same = entry["sha256"] == row["approved_sha256"]
        elif entry.get("file") is not None:
            same = entry["file"] == row["file"]
        elif single:
            same = True  # An old single-clip record: the workspace has exactly one clip.
        else:
            raise ValueError(f"publications.json has a {platform} record without file or sha256.")
        if not same:
            continue
        state = entry.get("status", entry.get("state"))
        if state is None and isinstance(entry.get("published"), bool):
            state = "published" if entry["published"] else "draft"
        if state in PRIOR_DONE:
            return str(state)
        if state not in PRIOR_NOT_POSTED:
            raise ValueError(
                f"publications.json has an unresolved {platform} record: {row['file']}"
            )
    return None


def _browser_outcome(work: Path, row: dict[str, Any]) -> str | None:
    """Browser receipts name an Instagram username, while ``--account`` is a provider account
    identifier with no verified mapping to it, so any browser receipt for these bytes blocks."""
    from zunara import browser  # Imported here: the browser adapter imports this module.

    state = browser.prior_outcome(work, row["approved_sha256"])
    return f"{browser.LEDGER} records {state}" if state else None


def plan(
    work: Path, only: set[str], platforms: list[str], profile: str, expected: dict[str, str]
) -> list[dict[str, Any]]:
    """Offline preflight under the workspace lock: verify media and find duplicates."""
    if not SAFE_NAME.fullmatch(profile):
        raise ValueError("--profile must be an Upload-Post profile username.")
    ledger = {
        (r["profile"], r["platform"], r["approved_sha256"]): r for r in load_ledger(work)["records"]
    }
    items = []
    rows = queue.load(work)
    prior = _prior_entries(work)
    for row in queue.select(rows, only):
        queue.verify_approved(work, row)
        caption = row.get("description", "").strip() or row["title"]
        for platform in platforms:
            item = {"file": row["file"], "platform": platform, "account": expected[platform]}
            stored = ledger.get((profile, platform, row["approved_sha256"]))
            earlier = _prior_outcome(prior, row, platform, len(rows) == 1)
            if stored:
                # Same profile, platform and bytes. The account it went to is reported as
                # recorded; a different account today is flagged, never treated as a match.
                item.update(
                    action="skip",
                    reason=f"recorded in {LEDGER}: {stored['state']}",
                    recorded_account=stored.get("account"),
                    account_changed=stored.get("account") != expected[platform],
                )
            elif platform == "youtube" and row.get("youtube_id"):
                item.update(action="skip", reason="queue.json already has a youtube_id")
            elif earlier:
                item.update(action="skip", reason=f"publications.json records {earlier}")
            elif platform == "instagram" and (browsed := _browser_outcome(work, row)):
                item.update(action="skip", reason=browsed)
            elif platform == "x" and len(caption) > X_LIMIT:
                raise ValueError(f"X text exceeds {X_LIMIT} characters: {row['file']}")
            else:
                item.update(action="upload")
            items.append({**item, "_row": row})
    return items


def preflight(client: Client, profile: str, expected: dict[str, str], platforms: set[str]) -> str:
    """Check the plan, the exact profile and every connected account. Returns the plan name."""
    plan_name = _plan(_get(client, "/uploadposts/me").get("plan"))
    known = " ".join((plan_name or "").split()).lower()
    if "tiktok" in platforms and known not in TIKTOK_PLANS:
        raise ValueError(
            "TikTok needs an Upload-Post plan known to include it (Basic, Professional, "
            "Advanced or Business); the current plan is Free or unrecognized."
        )
    found = sanitize_profile(
        _get(client, f"/uploadposts/users/{quote(profile, safe='')}").get("profile")
    )
    if found["username"] != profile:
        raise ValueError("Upload-Post returned a different profile than --profile.")
    for platform in sorted(platforms):
        account = found["accounts"].get(platform) or {}
        ids = {i.lstrip("@") for i in (account.get("username"), account.get("handle")) if i}
        if not ids:
            raise ValueError(f"No {platform} account is connected to profile {profile}.")
        if account.get("reauth_required") is True:
            raise ValueError(f"The {platform} account needs reauthorization in Upload-Post.")
        if expected[platform] not in ids:
            raise ValueError(f"The connected {platform} account does not match --account.")
    return plan_name or "unknown"


def _content(
    row: dict[str, Any], platform: str, privacy: str, language: str | None
) -> dict[str, str]:
    """Platform-specific fields that will be sent; the preview shows exactly these."""
    caption = row.get("description", "").strip() or row["title"]
    if platform == "instagram":
        return {"instagram_title": caption, "media_type": "REELS"}
    if platform == "tiktok":
        return {
            "tiktok_title": caption,
            "post_mode": "DIRECT_POST",
            "privacy_level": "PUBLIC_TO_EVERYONE",
            # Fail rather than silently land in the TikTok inbox as a draft.
            "disable_inbox_fallback": "true",
        }
    if platform == "x":
        return {"x_title": caption}
    fields = {
        "youtube_title": row["title"],
        "youtube_description": row.get("description", "") or row["title"],
        "privacyStatus": privacy,
    }
    return {**fields, "defaultLanguage": language} if language else fields


def _form(
    row: dict[str, Any],
    profile: str,
    platforms: list[str],
    schedule: datetime | None,
    privacy: str,
    language: str | None,
) -> list[tuple[str, str]]:
    fields = [("user", profile), ("async_upload", "true")]
    fields += [("platform[]", p) for p in platforms]
    fields += [("title", row["title"])]
    for platform in platforms:
        fields += list(_content(row, platform, privacy, language).items())
    if schedule:
        fields += [("scheduled_date", schedule.isoformat().replace("+00:00", "Z"))]
    return fields


POST_HOSTS = {
    "instagram": {"www.instagram.com", "instagram.com"},
    "tiktok": {"www.tiktok.com", "tiktok.com", "m.tiktok.com"},
    "youtube": {"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"},
    "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
}


def _safe_url(platform: str, value: object) -> str | None:
    """Keep only a plain public post URL on the platform's own host; drop anything else."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or host not in POST_HOSTS[platform]
        or parts.username
        or parts.password
        or port is not None
        or parts.fragment
        or not re.fullmatch(r"/[A-Za-z0-9/_.@-]{1,200}", parts.path)
    ):
        return None
    if parts.query:
        # Only a YouTube watch link may carry a query, and only its video ID.
        if parts.path != "/watch" or not re.fullmatch(r"v=[A-Za-z0-9_-]{11}", parts.query):
            return None
        return f"https://{host}/watch?{parts.query}"
    return f"https://{host}{parts.path}"


def apply_results(records: dict[str, dict[str, Any]], results: object) -> None:
    """Merge per-platform results (list or dict form) into ledger records.

    Only a success with a post ID or URL becomes ``completed``: a provider confirmation, not
    proof of public playback. A success without them (e.g. TikTok moderation) stays accepted.
    """
    if isinstance(results, dict):
        results = [{**v, "platform": k} for k, v in results.items() if isinstance(v, dict)]
    if not isinstance(results, list):
        return
    for result in results:
        if not isinstance(result, dict) or result.get("platform") not in records:
            continue
        record = records[result["platform"]]
        post_id = result.get("platform_post_id") or result.get("post_id")
        post_id = post_id if isinstance(post_id, str) and SAFE_ID.fullmatch(post_id) else None
        url = _safe_url(result["platform"], result.get("post_url") or result.get("url"))
        # TikTok inbox drafts are flagged, or carry an ID prefixed "v_inbox_file".
        ids = [result.get(k) for k in ("post_id", "platform_post_id", "publish_id", "id")]
        draft = result.get("fallback_to_inbox") is True or any(
            isinstance(i, str) and i.startswith("v_inbox_file") for i in ids
        )
        if result.get("success") is True and draft:
            # Delivered to the TikTok inbox as a draft: not published, never "completed".
            record.update(state="accepted", delivery="draft")
        elif result.get("success") is True and (post_id or url):
            record.update(state="completed", platform_post_id=post_id, post_url=url)
        elif result.get("success") is True and record["state"] in {"unknown", "submitting"}:
            record.update(state="accepted")
        elif result.get("success") is False:
            record.update(state="failed")
        record["updated_at"] = _now()


def _submit(
    work: Path,
    client: Client,
    ledger: dict[str, Any],
    records: dict[str, dict[str, Any]],
    form: list[tuple[str, str]],
    row: dict[str, Any],
    request_id: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="zunara-social-") as folder:
        # Snapshot and re-verify the approved bytes before anything is recorded or sent.
        snapshot = queue.copy_approved(work, row, Path(folder) / row["file"])
        ledger["records"].extend(records.values())
        _save_ledger(work, ledger)  # Recorded before the POST: a crash leaves "submitting".
        data: Any = None
        try:
            with snapshot.open("rb") as video:
                data = client.call(
                    "POST",
                    "/upload",
                    upload=True,
                    data=form,
                    files={"video": (row["file"], video, "video/mp4")},
                    headers={"Idempotency-Key": request_id},
                )
        except ValueError:
            # A definite 4xx rejection: nothing was accepted, so the records are released.
            ledger["records"] = [r for r in ledger["records"] if r not in records.values()]
            _save_ledger(work, ledger)
            raise
        except Unknown:
            pass
    reply = data if isinstance(data, dict) else {}
    returned, job_id = reply.get("request_id"), reply.get("job_id")
    for record in records.values():
        record["updated_at"] = _now()
        if isinstance(returned, str) and SAFE_ID.fullmatch(returned):
            record.update(state="accepted", request_id=returned)
        elif isinstance(job_id, str) and SAFE_ID.fullmatch(job_id):
            record.update(state="scheduled", job_id=job_id)
        elif isinstance(reply.get("results"), dict):
            record.update(state="accepted")
        else:
            record.update(state="unknown")
    apply_results(records, reply.get("results"))
    _save_ledger(work, ledger)
    if any(r["state"] == "unknown" for r in records.values()):
        raise ValueError(
            f"Upload outcome unknown for {row['file']}; it will not be retried. "
            f"Run 'zunara social status --refresh' to look up request {request_id}."
        )


def publish(
    work: Path,
    client: Client,
    items: list[dict[str, Any]],
    *,
    profile: str,
    expected: dict[str, str],
    schedule: datetime | None,
    privacy: str,
    language: str | None,
    execute: bool,
) -> dict[str, Any]:
    """Preview (default) or execute uploads. The caller holds the workspace lock."""
    if language is not None and not LANGUAGE.fullmatch(language):
        raise ValueError("--language must be a BCP-47 code such as es or en-US.")
    pending = [i for i in items if i["action"] == "upload"]
    plan_name = preflight(client, profile, expected, {i["platform"] for i in pending})
    public = []
    for item in items:
        shown = {k: v for k, v in item.items() if k != "_row"}
        if item["action"] == "upload":
            shown["fields"] = _content(item["_row"], item["platform"], privacy, language)
            shown["scheduled_at"] = schedule.isoformat() if schedule else None
        public.append(shown)
    if not execute:
        return {"mode": "preview", "plan": plan_name, "profile": profile, "items": public}
    ledger = load_ledger(work)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in pending:
        by_file.setdefault(item["file"], []).append(item)
    for group in by_file.values():
        row = group[0]["_row"]
        platforms = [i["platform"] for i in group]
        # Client-chosen ID, saved before the POST, so a lost response can still be looked up.
        request_id = uuid.uuid4().hex
        records = {}
        for item in group:
            records[item["platform"]] = {
                "profile": profile,
                "platform": item["platform"],
                "approved_sha256": row["approved_sha256"],
                "file": row["file"],
                "account": item["account"],
                "state": "submitting",
                "request_id": request_id,
                "idempotency_key": request_id,
                "scheduled_at": schedule.isoformat() if schedule else None,
                **(
                    {"privacy": privacy, "language": language}
                    if item["platform"] == "youtube"
                    else {}
                ),
                "created_at": _now(),
                "updated_at": _now(),
            }
        form = _form(row, profile, platforms, schedule, privacy, language)
        form.append(("request_id", request_id))
        _submit(work, client, ledger, records, form, row, request_id)
    keys = {(i["file"], i["platform"]) for i in items}
    done = [
        r
        for r in ledger["records"]
        if (r["file"], r["platform"]) in keys and r["profile"] == profile
    ]
    return {
        "mode": "execute",
        "plan": plan_name,
        "profile": profile,
        "items": public,
        "records": done,
    }


def status(work: Path, client: Client | None = None) -> dict[str, Any]:
    """Return ledger records; with a client, look up unfinished ones (GET only, never a POST).

    Records left ``unknown`` or ``submitting`` are looked up by the request ID saved before
    their POST, so a lost response can be resolved without uploading again.
    """
    ledger = load_ledger(work)
    if client is None:
        return {"records": ledger["records"]}
    groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for record in ledger["records"]:
        if record["state"] in {"accepted", "unknown", "submitting"}:
            key = ("request_id", str(record.get("request_id") or ""))
        elif record["state"] == "scheduled":
            key = ("job_id", str(record.get("job_id") or ""))
        else:
            continue
        if SAFE_ID.fullmatch(key[1]):
            groups.setdefault(key, {})[record["platform"]] = record
    errors = []
    for (field, value), records in groups.items():
        try:
            reply = _get(client, "/uploadposts/status", **{field: value})
        except ValueError as error:
            errors.append({field: value, "error": str(error)})
            continue
        echoed = reply.get(field)
        if (
            reply.get("status") not in {"pending", "in_progress", "completed"}
            or reply.get("success") is False
            or (echoed is not None and echoed != value)
        ):
            errors.append({field: value, "error": "Unrecognized status response; left unresolved."})
            continue
        apply_results(records, reply.get("results"))
        for record in records.values():
            if record["state"] in {"unknown", "submitting"}:
                # The provider knows this request, so it was received.
                record.update(state="accepted", updated_at=_now())
    if groups:
        _save_ledger(work, ledger)
    return {"records": ledger["records"], "refresh_errors": errors}
