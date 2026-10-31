"""Unattended Instagram browser publisher using a dedicated, Zunara-managed Chromium profile.

A person connects the profile once with ``browser login`` (a headed window where they type their
own credentials and resolve any checkpoint). Afterwards ``browser publish --execute`` runs without
an agent: it verifies the logged-in account from the page's own profile link, attaches a verified
snapshot of the approved clip, fills the caption and clicks the exact Share control.

Safety rules:

* Receipts in ``out/browser-publications.json`` are saved before the media is attached and again
  before Share is clicked, so a crash or lost confirmation can never cause a second post.
* A missing or ambiguous control stops the run (``ui_changed``); nothing is clicked by guess.
* Login, checkpoints and captchas are left to the person. There are no stealth flags, solvers,
  cookie extraction, CDP attachment or reuse of an existing browser profile.
* Playwright is optional and imported lazily; importing this module never opens a browser.
"""

import contextlib
import hashlib
import importlib.util
import json
import re
import shlex
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from filelock import FileLock, Timeout

from zunara import instagram, queue
from zunara.storage import write_json

SUPPORTED = ("instagram",)
KNOWN_PLATFORMS = ("instagram", "tiktok", "youtube", "x")
LEDGER = "browser-publications.json"
LEDGER_VERSION = 1
PROFILE_ROOT = "browser-profiles"
MARKER = ".zunara-browser-profile.json"
HOME = "https://www.instagram.com/"
LOGIN_PAGE = "https://www.instagram.com/accounts/login/"
HOSTS = {"www.instagram.com", "instagram.com"}
STEP_TIMEOUT = 30.0
SHARE_TIMEOUT = 300.0
LOGIN_TIMEOUT = 600.0
TIMEOUT_RANGE = (30.0, 3600.0)
SHA256 = re.compile(r"[0-9a-f]{64}")
STATES = (
    "preflight",  # Saved before the browser opens; nothing has been attached.
    "stopped",  # Stopped before Share; see ``media_selected`` for whether bytes were attached.
    "composing",  # Saved before the snapshot is attached; Share has not been clicked.
    "sharing",  # Saved immediately before Share is clicked; outcome unknown until observed.
    "unknown",  # Share was clicked but success was not observed. Never retried automatically.
    "published",  # Success confirmation observed on the page.
)
STATUSES = (
    "login_required",
    "checkpoint_required",
    "account_mismatch",
    "ui_changed",
    "browser_unavailable",
    "connection_required",
    "not_attempted",
    "unknown",
)
CONNECTIONS = ("dedicated", "chrome")
# Chromium user-data roots that belong to the person's own browsers, never to Zunara.
SYSTEM_PROFILES = (
    "~/.config/google-chrome",
    "~/.config/google-chrome-beta",
    "~/.config/google-chrome-unstable",
    "~/.config/chromium",
    "~/.config/BraveSoftware",
    "~/.config/microsoft-edge",
    "~/.config/vivaldi",
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Chromium",
    "~/Library/Application Support/BraveSoftware",
    "~/Library/Application Support/Microsoft Edge",
    "~/AppData/Local/Google/Chrome/User Data",
    "~/AppData/Local/Chromium/User Data",
    "~/AppData/Local/Microsoft/Edge/User Data",
    "~/snap/chromium",
)

# Accessible names, English and Spanish. Spanish labels for the profile link, create link,
# post menu item, dialog title and file button were observed read-only in the live web UI
# (October 2026); the remaining labels are unverified and fail closed when absent.
PROFILE_LINK = re.compile(r"(?:^|\s)(?:Profile|Perfil)$")
PROFILE_HREF = re.compile(r"/([A-Za-z0-9._]{1,30})/?")
CREATE_LINK = re.compile(
    r"^(?:(?:New post|Nueva publicación)(?:\s+(?:Create|Crear))?|Create|Crear)$"
)
POST_ITEM = re.compile(r"^(?:Post|Publicación)(?:\s+(?:Post|Publicación))?$")
DIALOG_TITLE = re.compile(r"^(?:Create new post|Crear nueva publicación)$")
SELECT_FILE = re.compile(
    r"^(?:Select from computer|Seleccionar del ordenador|Seleccionar desde la computadora)$"
)
NEXT = re.compile(r"^(?:Next|Siguiente)$")
ACKNOWLEDGE = re.compile(r"^(?:OK|Aceptar)$")
# The only dialog whose OK button the runner may press: Instagram's "videos are now reels" note.
REEL_INFO = re.compile(
    r"(?:Video posts are now shared as reels|"
    r"(?:Ahora )?las publicaciones de (?:video|vídeo) (?:ahora )?se comparten como reels)",
    re.IGNORECASE,
)
SELECT_CROP = re.compile(r"^(?:Select crop|Seleccionar recorte)$")
ORIGINAL = re.compile(r"^(?:Original)(?:\s.*)?$")
CAPTION_BOX = re.compile(
    r"^(?:Write a caption|Escribe un pie de foto|Escribe una descripción)(?:\.\.\.|…)?$"
)
SHARE = re.compile(r"^(?:Share|Compartir)$")
SUCCESS = re.compile(
    r"(?:Your (?:reel|post) (?:has been|was) shared|(?:Reel|Post) shared|"
    r"Se ha compartido (?:el|tu) (?:reel|publicación)|"
    r"Se compartió (?:el|tu) (?:reel|publicación)|(?:Reel|Publicación) compartid[oa])",
    re.IGNORECASE,
)
SHARE_ERROR = re.compile(r"(?:couldn.t be shared|could not be shared|no se pudo compartir)", re.I)
CHECKPOINT_PATHS = ("/challenge", "/checkpoint", "/two_factor", "/accounts/suspended", "/auth_")
CAPTCHA_FRAMES = 'iframe[src*="captcha" i], iframe[title*="captcha" i], iframe[src*="arkose" i]'

MESSAGES = {
    "login_required": (
        "Instagram needs a login for this browser profile. Run 'zunara browser login "
        "--platform instagram --username {username}' and sign in in that window."
    ),
    "checkpoint_required": (
        "Instagram is showing a checkpoint, verification or captcha. Run 'zunara browser login "
        "--platform instagram --username {username}' and resolve it yourself; Zunara never "
        "solves it."
    ),
    "account_mismatch": (
        "The browser profile is logged into a different Instagram account than --username."
    ),
    "browser_unavailable": (
        "Chromium could not start. Install the browser extra and run "
        "'playwright install chromium', then check 'zunara browser doctor'."
    ),
}


class BrowserStop(Exception):
    """A deliberate, sanitized stop. ``status`` is one of ``STATUSES``."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class InstagramUI(Protocol):
    """The deterministic page operations the flow needs; tests provide fakes."""

    def open(self, url: str) -> None: ...
    def blocker(self) -> str | None: ...
    def active_username(self, timeout: float) -> str | None: ...
    def open_composer(self) -> None: ...
    def attach(self, path: Path) -> None: ...
    def keep_original_aspect(self) -> None: ...
    def advance_to_caption(self) -> None: ...
    def set_caption(self, text: str) -> None: ...
    def share(self) -> None: ...
    def wait_for_outcome(self, timeout: float) -> tuple[str, str | None]: ...
    def pause(self, seconds: float) -> None: ...


Connect = Callable[[Path, bool], AbstractContextManager[InstagramUI]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def check_platform(platform: str) -> str:
    if platform in SUPPORTED:
        return platform
    if platform in KNOWN_PLATFORMS:
        raise ValueError(
            f"Browser publishing is not implemented for {platform}; only instagram is supported."
        )
    raise ValueError(f"Unknown platform: {platform}. Browser publishing supports only instagram.")


def check_username(username: str) -> str:
    handle = username.removeprefix("@")
    if not instagram.USERNAME.fullmatch(handle):
        raise ValueError("--username must be an Instagram username.")
    return handle.lower()


def check_timeout(value: float, name: str) -> float:
    low, high = TIMEOUT_RANGE
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g} seconds.")
    return value


# ---------------------------------------------------------------------------------------------
# Dedicated profile management
# ---------------------------------------------------------------------------------------------


def _refuse_system_profile(path: Path) -> None:
    resolved = path.resolve()
    for raw in SYSTEM_PROFILES:
        system = Path(raw).expanduser().resolve()
        if resolved == system or system in resolved.parents:
            raise ValueError("Refusing to use a personal browser profile; use a Zunara profile.")
    for parent in resolved.parents:
        if (parent / "Local State").exists():
            raise ValueError("Refusing a profile nested inside another browser's user data.")


def _private_dirs(base: Path, parts: tuple[str, ...], *, create: bool) -> Path:
    """Walk ``base/parts`` refusing symlinks; optionally create owner-only directories."""
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Browser profile paths must not be symlinks.")
        if not current.exists():
            if not create:
                return base.joinpath(*parts)  # Nothing below a missing part can be a symlink.
            current.mkdir(parents=True, mode=0o700)
        if not current.is_dir():
            raise ValueError("Browser profile path is not a directory.")
        if create:
            current.chmod(0o700)
    return current


def profile_dir(config_dir: Path, platform: str, username: str, *, create: bool) -> Path:
    """Return the dedicated profile directory, rejecting symlinks and foreign profiles."""
    parts = (PROFILE_ROOT, check_platform(platform), check_username(username))
    path = _private_dirs(config_dir, parts, create=False)
    _refuse_system_profile(path)
    marker = path / MARKER
    if path.exists() and (marker.is_symlink() or (any(path.iterdir()) and not marker.is_file())):
        raise ValueError("The browser profile directory was not created by Zunara.")
    if create:
        _private_dirs(config_dir, parts, create=True)
        if not marker.exists():
            write_json(marker, {"platform": parts[1], "username": parts[2], "created_at": _now()})
    return path


@contextlib.contextmanager
def profile_lock(config_dir: Path, platform: str, username: str) -> Iterator[None]:
    parent = _private_dirs(config_dir, (PROFILE_ROOT, check_platform(platform)), create=True)
    lock = FileLock(str(parent / f".{check_username(username)}.lock"), timeout=0)
    try:
        with lock:
            yield
    except Timeout as error:
        raise ValueError("The browser profile is in use by another Zunara command.") from error


def _profiles(config_dir: Path) -> list[dict[str, Any]]:
    root = config_dir / PROFILE_ROOT / "instagram"
    if root.is_symlink() or not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or not path.is_dir():
            continue
        found.append(
            {
                "platform": "instagram",
                "username": path.name,
                "managed": (path / MARKER).is_file() and not path.is_symlink(),
                "private": not stat.S_IMODE(path.stat().st_mode) & 0o077,
            }
        )
    return found


def doctor(config_dir: Path) -> dict[str, Any]:
    """Report local readiness without launching a browser or reading cookies."""
    installed = importlib.util.find_spec("playwright") is not None
    chromium = False
    if installed:
        with contextlib.suppress(Exception):
            from playwright.sync_api import sync_playwright

            with sync_playwright() as driver:
                chromium = Path(driver.chromium.executable_path).is_file()
    return {
        "platforms": list(SUPPORTED),
        "playwright_installed": installed,
        "chromium_installed": chromium,
        "profile_root": str(config_dir / PROFILE_ROOT),
        "profiles": _profiles(config_dir),
        "live_session": "unknown; doctor opens no browser",
    }


# ---------------------------------------------------------------------------------------------
# Playwright implementation of the page operations
# ---------------------------------------------------------------------------------------------


@contextlib.contextmanager
def playwright_page(
    profile: Path, headed: bool, *, prepare: Callable[[Any], None] | None = None
) -> Iterator[Any]:
    """Launch bundled Chromium with the dedicated persistent profile and yield its page.

    ``prepare`` is an internal hook for tests (e.g. routing fixture pages); the CLI never sets it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise BrowserStop("browser_unavailable", MESSAGES["browser_unavailable"]) from None
    with sync_playwright() as driver:
        try:
            context = driver.chromium.launch_persistent_context(
                str(profile),
                headless=not headed,
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
            )
        except Exception:
            raise BrowserStop("browser_unavailable", MESSAGES["browser_unavailable"]) from None
        try:
            if prepare is not None:
                prepare(context)
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            with contextlib.suppress(Exception):
                context.close()


class PlaywrightInstagram:
    """Deterministic Instagram web operations. Every click targets exactly one visible control."""

    def __init__(self, page: Any, step_timeout: float = STEP_TIMEOUT) -> None:
        self.page = page
        self.step = step_timeout

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def open(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.step * 1000)

    def _dialog(self) -> Any:
        return self.page.locator('[role="dialog"]')

    def _visible(self, locator: Any) -> list[Any]:
        # One count of visible matches: a single round trip, also through the Chrome bridge.
        shown = locator.filter(visible=True)
        return [shown.nth(i) for i in range(shown.count())]

    def _one(self, find: Callable[[], Any], what: str, timeout: float | None = None) -> Any:
        deadline = time.monotonic() + (self.step if timeout is None else timeout)
        while True:
            found = self._visible(find())
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise BrowserStop("ui_changed", f"Several {what} controls were found; stopped.")
            if time.monotonic() >= deadline:
                raise BrowserStop("ui_changed", f"The {what} control was not found; stopped.")
            self.pause(0.25)

    def blocker(self) -> str | None:
        parts = urlsplit(self.page.url)
        if parts.hostname not in HOSTS:
            return "checkpoint_required"
        if parts.path.startswith("/accounts/login"):
            return "login_required"
        if parts.path.startswith(CHECKPOINT_PATHS):
            return "checkpoint_required"
        if self._visible(self.page.locator(CAPTCHA_FRAMES)):
            return "checkpoint_required"
        if self._visible(self.page.locator('input[name="password"]')):
            return "login_required"
        return None

    def active_username(self, timeout: float) -> str | None:
        """Read the handle from the authenticated navigation's own profile link."""
        deadline = time.monotonic() + timeout
        while True:
            links = self._visible(self.page.get_by_role("link", name=PROFILE_LINK))
            handles = set()
            for link in links:
                match = PROFILE_HREF.fullmatch(link.get_attribute("href") or "")
                if match:
                    handles.add(match.group(1).lower())
            if len(handles) > 1:
                raise BrowserStop("ui_changed", "Several profile links were found; stopped.")
            if handles:
                return handles.pop()
            if time.monotonic() >= deadline or self.blocker():
                return None
            self.pause(0.5)

    def _file_input(self) -> Any:
        return self._dialog().locator('input[type="file"]')

    def open_composer(self) -> None:
        self._one(lambda: self.page.get_by_role("link", name=CREATE_LINK), "create").click()
        deadline = time.monotonic() + self.step
        while not self._visible(self._dialog().get_by_role("heading", name=DIALOG_TITLE)):
            items = self._visible(self.page.get_by_role("link", name=POST_ITEM))
            if len(items) == 1:
                items[0].click()
            elif len(items) > 1:
                raise BrowserStop("ui_changed", "Several post menu items were found; stopped.")
            if time.monotonic() >= deadline:
                raise BrowserStop("ui_changed", "The create-post dialog did not open; stopped.")
            self.pause(0.25)
        self._one(lambda: self._dialog().get_by_role("button", name=SELECT_FILE), "select file")
        if self._file_input().count() != 1:
            raise BrowserStop("ui_changed", "The dialog has no single file input; stopped.")

    def attach(self, path: Path) -> None:
        self._file_input().set_input_files(str(path), timeout=self.step * 1000)

    def _caption(self) -> Any:
        return self._dialog().get_by_role("textbox", name=CAPTION_BOX)

    def _acknowledge_reel_info(self) -> bool:
        """Press OK only inside Instagram's known reels notice; never an arbitrary OK."""
        notice = self.page.get_by_role("dialog").filter(has_text=REEL_INFO)
        if not self._visible(notice):
            return False
        buttons = self._visible(notice.last.get_by_role("button", name=ACKNOWLEDGE))
        if len(buttons) != 1:
            raise BrowserStop("ui_changed", "The reels notice has no single OK control; stopped.")
        buttons[0].click()
        return True

    def keep_original_aspect(self) -> None:
        """Select the Original crop so vertical media is not cropped square; else stop."""
        deadline = time.monotonic() + self.step
        while True:
            self._acknowledge_reel_info()
            crop = self._visible(self._dialog().get_by_role("button", name=SELECT_CROP))
            if len(crop) == 1:
                break
            if len(crop) > 1:
                raise BrowserStop("ui_changed", "Several crop controls were found; stopped.")
            if time.monotonic() >= deadline:
                raise BrowserStop(
                    "ui_changed", "The crop control was not found; original aspect not ensured."
                )
            self.pause(0.25)
        crop[0].click()
        option = self._one(
            lambda: self._dialog().get_by_role("button", name=ORIGINAL), "Original crop"
        )
        option.click()
        for attribute in ("aria-pressed", "aria-checked", "aria-selected"):
            value = None
            with contextlib.suppress(Exception):  # The option may close after selection.
                value = option.get_attribute(attribute, timeout=1000)
            if value is not None and value != "true":
                raise BrowserStop("ui_changed", "The Original crop was not selected; stopped.")

    def advance_to_caption(self) -> None:
        deadline = time.monotonic() + self.step * 3
        clicks = 0
        while not self._visible(self._caption()):
            if not self._acknowledge_reel_info():
                buttons = self._visible(self._dialog().get_by_role("button", name=NEXT))
                if len(buttons) > 1:
                    raise BrowserStop("ui_changed", "Several Next controls were found; stopped.")
                if len(buttons) == 1 and clicks < 2:
                    buttons[0].click(timeout=self.step * 1000)
                    clicks += 1
            if time.monotonic() >= deadline:
                raise BrowserStop("ui_changed", "The caption field did not appear; stopped.")
            self.pause(0.5)

    def set_caption(self, text: str) -> None:
        box = self._one(self._caption, "caption")
        box.click()
        box.fill(text)
        attribute = box.get_attribute("contenteditable")
        editable = attribute is not None and attribute.lower() in {"", "true"}
        tag_value = box.input_value() if not editable else box.inner_text()
        if " ".join(tag_value.split()) != " ".join(text.split()):
            raise BrowserStop("ui_changed", "The caption field did not keep the text; stopped.")

    def share(self) -> None:
        button = self._one(
            lambda: self._dialog().get_by_role("button", name=SHARE, exact=True), "Share"
        )
        if not button.is_enabled():
            raise BrowserStop("ui_changed", "The Share control is disabled; stopped.")
        button.click(timeout=self.step * 1000)

    def _permalink(self) -> str | None:
        links = self._dialog().locator('a[href*="/reel/"], a[href*="/p/"]')
        found = set()
        for index in range(links.count()):
            href = links.nth(index).get_attribute("href") or ""
            safe = instagram._safe_permalink(urljoin(HOME, href))
            if safe:
                found.add(safe)
        return found.pop() if len(found) == 1 else None

    def wait_for_outcome(self, timeout: float) -> tuple[str, str | None]:
        deadline = time.monotonic() + timeout
        while True:
            # Only a confirmation heading in the composer dialog counts; caption or other page
            # text that happens to contain the same words is never proof of publication.
            if self._visible(self._dialog().get_by_role("heading", name=SUCCESS)):
                return "published", self._permalink()
            if self._visible(self.page.get_by_text(SHARE_ERROR)):
                return "error_shown", None
            blocker = self.blocker()
            if blocker:
                return blocker, None  # Recorded as unknown at once; never solved or retried.
            if time.monotonic() >= deadline:
                return "timeout", None
            self.pause(1.0)


def connect_playwright(profile: Path, headed: bool) -> AbstractContextManager[InstagramUI]:
    @contextlib.contextmanager
    def session() -> Iterator[InstagramUI]:
        with playwright_page(profile, headed) as page:
            yield PlaywrightInstagram(page)

    return session()


# ---------------------------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------------------------


def load_ledger(work: Path) -> dict[str, Any]:
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
            or record.get("platform") not in SUPPORTED
            or not instagram.USERNAME.fullmatch(str(record.get("username")))
            or not SHA256.fullmatch(str(record.get("approved_sha256")))
            or record.get("state") not in STATES
            or record.get("status") not in (None, *STATUSES)
            or not isinstance(record.get("media_selected"), bool)
            or not isinstance(record.get("share_clicked"), bool)
            or not isinstance(record.get("file"), str)
            or record.get("connection", "dedicated") not in CONNECTIONS
        ):
            raise ValueError(f"{LEDGER} contains an invalid record.")
        key = (record["platform"], record["username"], record["approved_sha256"])
        if key in keys:
            raise ValueError(f"{LEDGER} contains a duplicate record.")
        keys.add(key)
    return data


def _save(work: Path, ledger: dict[str, Any]) -> None:
    write_json(work / "out" / LEDGER, ledger)  # mkstemp staging: owner-only file


def _blocking(record: dict[str, Any]) -> bool:
    """True when the record means a post may exist (or may still appear)."""
    state = record["state"]
    return state in {"published", "sharing", "unknown", "composing"} or (
        state == "stopped" and record["media_selected"]
    )


def prior_outcome(work: Path, sha256: str, username: str | None = None) -> str | None:
    """Return a blocking browser receipt state for other adapters' duplicate checks."""
    for record in load_ledger(work)["records"]:
        if record["approved_sha256"] != sha256 or not _blocking(record):
            continue
        if username is None or record["username"].lower() == username.lower():
            return str(record["state"])
    return None


def _earlier(work: Path, rows: list[dict[str, Any]], row: dict[str, Any], username: str) -> str:
    for record in instagram.load_ledger(work)["records"]:
        if (
            record["approved_sha256"] == row["approved_sha256"]
            and str(record.get("username", "")).lower() == username
            and record["state"] not in {"failed", "expired"}
        ):
            return f"{instagram.LEDGER} records {record['state']}"
    return instagram._earlier(work, rows, row, username, include_browser=False) or ""


# ---------------------------------------------------------------------------------------------
# Plan, execute, login, status
# ---------------------------------------------------------------------------------------------


def plan(
    work: Path, only: set[str], username: str, *, retry_unshared: bool
) -> list[dict[str, Any]]:
    """Offline preflight under the workspace lock. Opens no browser."""
    if not only:
        raise ValueError("--only must name at least one clip.")
    receipts = {
        (r["username"], r["approved_sha256"]): r
        for r in load_ledger(work)["records"]
        if r["platform"] == "instagram"
    }
    rows = queue.load(work)
    chosen = queue.select(rows, only)
    for row in chosen:
        queue.verify_approved(work, row)
        instagram.caption(row)
    items = []
    for row in chosen:
        item: dict[str, Any] = {"file": row["file"], "platform": "instagram", "username": username}
        stored = receipts.get((username, row["approved_sha256"]))
        earlier = _earlier(work, rows, row, username)
        if stored and stored["state"] == "published":
            item.update(action="skip", reason=f"recorded in {LEDGER}: published")
        elif stored and stored["state"] in {"sharing", "unknown"}:
            item.update(
                action="blocked",
                reason=f"{LEDGER} records {stored['state']} after Share; reconcile manually",
            )
        elif stored and _blocking(stored) and not retry_unshared:
            item.update(
                action="blocked",
                reason=(
                    f"{LEDGER} records {stored['state']}: media was attached but Share was never "
                    "clicked; check for a draft, then rerun with --retry-unshared"
                ),
            )
        elif earlier:
            item.update(action="skip", reason=earlier)
        else:
            media = instagram.check_media(queue.verify_approved(work, row), row["file"])
            item.update(
                action="publish",
                caption=instagram.caption(row),
                media={k: media[k] for k in ("bytes", "duration_s", "width", "height", "codec")},
            )
            if stored:
                item["previous"] = {"state": stored["state"], "status": stored.get("status")}
        items.append({**item, "_row": row})
    return items


def public(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in i.items() if k != "_row"} for i in items]


def preview(
    config_dir: Path,
    work: Path,
    items: list[dict[str, Any]],
    username: str,
    connection: str = "dedicated",
) -> Any:
    stems = [Path(i["file"]).stem for i in items if i["action"] == "publish"]
    command = (
        f"zunara browser publish {shlex.quote(str(work))} --platform instagram "
        f"--username {username} --only {','.join(stems)} --connection {connection} --execute"
    )
    if connection == "chrome":
        # Nothing is contacted: the bridge and the Instagram session are checked on execution.
        where: dict[str, Any] = {"bridge": "unverified; preview contacts no browser"}
    else:
        profile = profile_dir(config_dir, "instagram", username, create=False)
        # A profile directory is not proof of a live session; execution verifies it on the page.
        where = {"profile_exists": (profile / MARKER).is_file()}
    return {
        "mode": "preview",
        "platform": "instagram",
        "username": username,
        "connection": connection,
        **where,
        "session": "unverified; preview opens no browser",
        "items": public(items),
        "next": [command] if stems else [],
    }


CHROME_MESSAGES = {
    "login_required": (
        "Instagram is not logged in to {username} in your main Chrome. Sign in there yourself, "
        "then run 'zunara browser connect --platform instagram --username {username}'."
    ),
    "checkpoint_required": (
        "Instagram is showing a checkpoint, verification or captcha in your main Chrome. "
        "Resolve it there yourself, then run 'zunara browser connect --platform instagram "
        "--username {username}'; Zunara never solves it."
    ),
    "account_mismatch": (
        "Your main Chrome is logged into a different Instagram account than --username."
    ),
    "connection_required": (
        "The Playwright Extension bridge to your main Chrome is not connected. Keep Chrome "
        "running and run 'zunara browser connect --platform instagram --username {username}'."
    ),
    "browser_unavailable": (
        "The Playwright CLI could not run. Install Node.js (npx) and check 'zunara browser doctor'."
    ),
}


def _message(status: str, username: str, fallback: str, connection: str = "dedicated") -> str:
    table = CHROME_MESSAGES if connection == "chrome" else MESSAGES
    return table.get(status, fallback).format(username=username)


def _verify_identity(ui: InstagramUI, username: str, connection: str = "dedicated") -> None:
    ui.open(HOME)
    blocker = ui.blocker()
    if blocker:
        raise BrowserStop(blocker, _message(blocker, username, "", connection))
    found = ui.active_username(STEP_TIMEOUT)
    if found is None:
        blocker = ui.blocker() or "ui_changed"
        raise BrowserStop(
            blocker,
            _message(blocker, username, "The profile link was not found; stopped.", connection),
        )
    if found != username:
        raise BrowserStop(
            "account_mismatch", _message("account_mismatch", username, "", connection)
        )


def _publish_one(
    work: Path,
    ledger: dict[str, Any],
    ui: InstagramUI,
    item: dict[str, Any],
    record: dict[str, Any],
    share_timeout: float,
) -> None:
    def mark(state: str, **fields: Any) -> None:
        record.update(state=state, updated_at=_now(), **fields)
        _save(work, ledger)

    row = item["_row"]
    with tempfile.TemporaryDirectory(prefix="zunara-browser-") as folder:
        # Only this verified, private snapshot of the approved bytes is ever given to the page.
        snapshot = queue.copy_approved(work, row, Path(folder) / row["file"])
        if snapshot.stat().st_size != item["media"]["bytes"]:
            raise ValueError(f"Media changed after preflight: {row['file']}")
        ui.open_composer()
        mark("composing", media_selected=True)  # Saved before any byte can leave the machine.
        ui.attach(snapshot)
        ui.keep_original_aspect()
        mark("composing", aspect="original")
        ui.advance_to_caption()
        ui.set_caption(item["caption"])
        mark("sharing", share_clicked=True)  # Saved before the irreversible click.
        ui.share()
        outcome, permalink = ui.wait_for_outcome(share_timeout)
    if outcome != "published":
        mark("unknown", status="unknown", observed=outcome)
        raise BrowserStop(
            "unknown",
            f"Share was clicked for {row['file']} but success was not observed ({outcome}). "
            "It will not be retried; check the account and reconcile the receipt manually.",
        )
    mark("published", status=None, permalink=permalink, published_at=_now())


def _stop_record(record: dict[str, Any], status: str) -> None:
    if record["share_clicked"]:
        record.update(state="unknown", status="unknown")
    elif record["state"] in {"preflight", "composing", "stopped"}:
        record.update(state="stopped", status=status)
    record["updated_at"] = _now()


def execute(
    work: Path,
    config_dir: Path,
    items: list[dict[str, Any]],
    *,
    username: str,
    headed: bool,
    share_timeout: float,
    connect: Connect | None = None,
    connection: str = "dedicated",
) -> dict[str, Any]:
    """Run the browser flow for the planned items. The caller holds the workspace lock.

    ``connection="dedicated"`` drives the Zunara profile; ``"chrome"`` drives a Zunara-owned tab
    in the person's main Chrome through the Playwright Extension bridge. Both share this
    lifecycle, receipts and page operations.
    """
    if connection not in CONNECTIONS:
        raise ValueError("--connection must be dedicated or chrome.")
    blocked = [i for i in items if i["action"] == "blocked"]
    if blocked:
        names = ", ".join(f"{i['file']} ({i['reason']})" for i in blocked)
        raise ValueError(f"Refusing to publish; unresolved browser receipts: {names}.")
    pending = [i for i in items if i["action"] == "publish"]
    if not pending:
        return {"mode": "nothing-to-do", "items": public(items)}
    ledger = load_ledger(work)
    if connection == "chrome":
        from zunara import browser_bridge  # Optional connector; imports no browser library.

        lock = browser_bridge.session_lock(config_dir)
        opener = connect or browser_bridge.connect_chrome(config_dir)
    else:
        lock = profile_lock(config_dir, "instagram", username)
        opener = connect or connect_playwright
    with lock:
        if connection == "chrome":
            profile = browser_bridge.bridge_dir(config_dir, create=True)
        else:
            profile = profile_dir(config_dir, "instagram", username, create=False)
        records = []
        for item in pending:
            sha = item["_row"]["approved_sha256"]
            ledger["records"] = [
                r
                for r in ledger["records"]
                if (r["platform"], r["username"], r["approved_sha256"])
                != ("instagram", username, sha)
            ]
            record = {
                "platform": "instagram",
                "username": username,
                "approved_sha256": sha,
                "file": item["file"],
                "caption_sha256": hashlib.sha256(item["caption"].encode()).hexdigest(),
                "state": "preflight",
                "status": None,
                "media_selected": False,
                "share_clicked": False,
                "permalink": None,
                "connection": connection,
                "headed": headed if connection == "dedicated" else None,
                "created_at": _now(),
                "updated_at": _now(),
            }
            ledger["records"].append(record)
            records.append(record)
        _save(work, ledger)  # Receipts exist before the browser opens.
        current: dict[str, Any] | None = None
        try:
            if connection == "dedicated" and not (profile / MARKER).is_file():
                raise BrowserStop("login_required", _message("login_required", username, ""))
            with opener(profile, headed) as ui:
                try:
                    for item, record in zip(pending, records, strict=True):
                        current = record
                        # Every item starts from a fresh home page with the identity re-read,
                        # which also dismisses the previous item's success dialog.
                        _verify_identity(ui, username, connection)
                        _publish_one(work, ledger, ui, item, record, share_timeout)
                except (BrowserStop, ValueError):
                    raise  # Our own sanitized stops, e.g. media changed since approval.
                except Exception:
                    # Raw driver errors may contain URLs or page text; never surface them.
                    status = "ui_changed"
                    with contextlib.suppress(Exception):
                        status = ui.blocker() or status
                    fallback = "The page did not respond as expected."
                    raise BrowserStop(
                        status, _message(status, username, fallback, connection)
                    ) from None
        except BrowserStop as stop:
            for record in records:
                if record is current:
                    _stop_record(record, stop.status)
                elif record["state"] == "preflight":
                    _stop_record(record, stop.status if current is None else "not_attempted")
            _save(work, ledger)
            attached = current is not None and current["media_selected"]
            note = (
                ""
                if attached
                else (
                    f" Nothing was attached for {current['file'] if current else 'any clip'}; "
                    "rerun with --execute when resolved."
                )
            )
            detail = str(stop) or _message(stop.status, username, "", connection)
            raise ValueError(
                f"Browser publish stopped ({stop.status}, connection {connection}): {detail}{note}"
            ) from None
    return {
        "mode": "execute",
        "platform": "instagram",
        "username": username,
        "connection": connection,
        "items": public(items),
        "records": records,
    }


def login(
    config_dir: Path, username: str, timeout: float, connect: Connect | None = None
) -> dict[str, Any]:
    """Open a headed window on the dedicated profile and wait for the person to log in."""
    with profile_lock(config_dir, "instagram", username):
        profile = profile_dir(config_dir, "instagram", username, create=True)
        try:
            with (connect or connect_playwright)(profile, True) as ui:
                ui.open(LOGIN_PAGE)
                deadline = time.monotonic() + timeout
                while True:
                    found = None if ui.blocker() else ui.active_username(2.0)
                    if found == username:
                        break
                    if found is not None:
                        raise BrowserStop("account_mismatch", MESSAGES["account_mismatch"])
                    if time.monotonic() >= deadline:
                        raise BrowserStop(
                            "login_required", f"Login was not completed within {timeout:g} s."
                        )
                    ui.pause(2.0)
            marker = profile / MARKER
            details = json.loads(marker.read_text(encoding="utf-8"))
            write_json(marker, {**details, "last_verified_login_at": _now()})
        except BrowserStop as stop:
            raise ValueError(f"Browser login stopped ({stop.status}): {stop}") from None
        except Exception:
            raise ValueError(
                "Browser login stopped: the window closed or stopped responding."
            ) from None
    return {
        "platform": "instagram",
        "username": username,
        "status": "connected",
        "profile": str(profile),
        "note": "The session lives only in this private profile; it may expire at any time.",
    }


def status(work: Path) -> dict[str, Any]:
    return {"records": load_ledger(work)["records"]}
