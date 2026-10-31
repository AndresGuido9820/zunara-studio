"""Main-Chrome connector: drive a Zunara-owned tab through Microsoft's Playwright Extension.

The person installs the official Playwright Extension in their everyday Chrome and stays logged in
to Instagram there. Zunara talks to it only through the pinned public ``@playwright/cli``
(``attach --extension=chrome``, ``tab-new``, ``run-code``, ``detach``) using a fixed session name
and a private working directory. It never reads cookies, storage exports or the CLI's internal
files, never lists or touches other tabs and never closes Chrome.

Each page operation used by :class:`zunara.browser.PlaywrightInstagram` becomes one ``run-code``
call. The code is a fixed interpreter; selectors, regular expressions and values are passed as
JSON data, never interpolated as code. Every call first checks that the current page is the tab
Zunara opened for this run (a marker set on the daemon's Page object), so an operation can never
fall through to one of the person's own tabs.
"""

import contextlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess  # nosec B404
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from filelock import FileLock, Timeout

from zunara import browser

CLI_PACKAGE = "@playwright/cli@0.1.22"
SESSION = "zunara-instagram"
BRIDGE_DIR = "browser-bridge"
# Names of the extension bridge token sources, not secrets.
TOKEN_ENV = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"  # nosec B105
TOKEN_FILE = "browser-extension-token"  # nosec B105
TOKEN = re.compile(r"[A-Za-z0-9_.+/=-]{16,512}")
CALL_MARGIN = 30.0
CALL_TIMEOUT = 60.0
ATTACH_TIMEOUT = 120.0
SNAPSHOTS = ".playwright-cli"
ROLES = {"link", "button", "dialog", "heading", "textbox"}
NOT_OPEN = "is not open"
NOT_OWNED = "zunara-tab-not-owned"

# A fixed interpreter. Only ``__SPEC__`` is replaced, with a JSON literal (ensure_ascii).
PROGRAM = """async (page) => {
  const spec = __SPEC__;
  if (spec.action === "ping") return true;
  if (spec.action === "claim") {
    // Only the blank tab this run just created, identified by its exact one-time URL.
    if (page.__zunaraTab !== undefined || page.url() !== spec.args.url) {
      throw new Error("__NOT_OWNED__");
    }
    page.__zunaraTab = spec.tab;
    return true;
  }
  if (page.__zunaraTab !== spec.tab) throw new Error("__NOT_OWNED__");
  if (spec.action === "release") { await page.close(); return true; }
  if (spec.action === "url") return page.url();
  if (spec.action === "goto") {
    await page.goto(spec.args.url, {waitUntil: spec.args.waitUntil, timeout: spec.args.timeout});
    return null;
  }
  const value = (v) => (v !== null && typeof v === "object") ? new RegExp(v.re, v.flags) : v;
  let target = page;
  for (const step of spec.chain) {
    if (step.op === "locator") target = target.locator(step.css);
    else if (step.op === "role") {
      const options = {};
      if (step.name !== null) options.name = value(step.name);
      if (step.exact !== null) options.exact = step.exact;
      target = target.getByRole(step.role, options);
    } else if (step.op === "text") target = target.getByText(value(step.text));
    else if (step.op === "filter") {
      const options = {};
      if (step.visible !== null) options.visible = step.visible;
      if (step.hasText !== null) options.hasText = value(step.hasText);
      target = target.filter(options);
    } else if (step.op === "nth") target = target.nth(step.index);
    else if (step.op === "last") target = target.last();
    else throw new Error("unsupported step");
  }
  const a = spec.args;
  switch (spec.action) {
    case "count": return await target.count();
    case "isVisible": return await target.isVisible();
    case "isEnabled": return await target.isEnabled({timeout: a.timeout});
    case "click": await target.click({timeout: a.timeout}); return null;
    case "fill": await target.fill(a.value, {timeout: a.timeout}); return null;
    case "getAttribute": return await target.getAttribute(a.name, {timeout: a.timeout});
    case "inputValue": return await target.inputValue({timeout: a.timeout});
    case "innerText": return await target.innerText({timeout: a.timeout});
    case "setInputFiles": await target.setInputFiles(a.path, {timeout: a.timeout}); return null;
  }
  throw new Error("unsupported action");
}""".replace("__NOT_OWNED__", NOT_OWNED)


class BridgeFailure(Exception):
    """A sanitized bridge error; raw CLI output is never included."""


def _stop(status: str) -> browser.BrowserStop:
    """A stop whose actionable text is filled in by the caller, which knows the username."""
    return browser.BrowserStop(status, "")


def _js_value(value: object) -> object:
    """Encode a string or a compiled regex as JSON data for the fixed interpreter."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, re.Pattern):
        return js_regex(value)
    raise ValueError("Unsupported selector value.")


def js_regex(pattern: re.Pattern[str]) -> dict[str, str]:
    """Translate a Python regex used by the selectors into JavaScript RegExp source and flags."""
    allowed = re.IGNORECASE | re.UNICODE
    if pattern.flags & ~allowed:
        raise ValueError("Selector regex uses flags that have no JavaScript equivalent.")
    source = pattern.pattern
    if re.search(r"\(\?P|\(\?#|\(\?[aiLmsux]|\\[AZ]", source):
        raise ValueError("Selector regex uses Python-only syntax.")
    return {"re": source, "flags": "i" if pattern.flags & re.IGNORECASE else ""}


def bridge_dir(config_dir: Path, *, create: bool) -> Path:
    path = browser._private_dirs(config_dir, (BRIDGE_DIR,), create=create)
    if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"The bridge directory must be private: chmod 700 {path}")
    return path


@contextlib.contextmanager
def session_lock(config_dir: Path) -> Iterator[None]:
    lock = FileLock(str(bridge_dir(config_dir, create=True) / ".session.lock"), timeout=0)
    try:
        with lock:
            yield
    except Timeout as error:
        raise ValueError("The Chrome bridge is in use by another Zunara command.") from error


def token_status(config_dir: Path) -> dict[str, Any]:
    """Describe the bridge token source without reading the token."""
    path = config_dir / TOKEN_FILE
    source = "environment" if os.environ.get(TOKEN_ENV, "").strip() else None
    private = None
    if path.is_symlink():
        private, source = False, source or "file"
    elif path.exists():
        private, source = not stat.S_IMODE(path.stat().st_mode) & 0o077, source or "file"
    return {"token_source": source, "token_file": str(path), "token_file_private": private}


def load_token(config_dir: Path) -> str | None:
    """Return the extension bridge token, or None when none is configured."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        path = config_dir / TOKEN_FILE
        if path.is_symlink():
            raise ValueError("The extension token file must not be a symlink.")
        if not path.exists():
            return None
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(f"The extension token file must be private: chmod 600 {path}")
        token = path.read_text(encoding="utf-8").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("The extension bridge token is malformed.")
    return token


Runner = Callable[..., subprocess.CompletedProcess[str]]


class Cli:
    """The pinned public Playwright CLI, invoked with an argv list and a private working directory."""

    def __init__(
        self,
        config_dir: Path,
        session: str = SESSION,
        *,
        runner: Runner = subprocess.run,
        command: list[str] | None = None,
    ) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", session):
            raise ValueError("Bridge session names use lowercase letters, digits and hyphens.")
        self.config_dir = config_dir
        self.session = session
        self.runner = runner
        self.command = command

    def _base(self) -> list[str]:
        if self.command is not None:
            return list(self.command)
        # POSIX only: on Windows npx is a .cmd shim run by a command shell, which must never
        # receive caption data. The dedicated-profile mode remains available there.
        npx = shutil.which("npx")
        if os.name != "posix" or npx is None:
            raise _stop("browser_unavailable")
        resolved = Path(npx).resolve()
        if resolved.suffix.lower() in {".cmd", ".bat", ".ps1"} or not resolved.is_file():
            raise _stop("browser_unavailable")
        return [npx, "--yes", "--package", CLI_PACKAGE, "playwright-cli"]

    def call(self, args: list[str], timeout: float, *, token: str | None = None) -> Any:
        """Run one CLI command with ``--json`` and return its decoded result."""
        argv = [*self._base(), f"-s={self.session}", "--json", *args]
        env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
        env["npm_config_update_notifier"] = "false"
        if token is not None:
            env[TOKEN_ENV] = token  # Only the attach subprocess receives the bridge capability.
        cwd = bridge_dir(self.config_dir, create=True)
        if (cwd / SNAPSHOTS).is_symlink():
            # The CLI writes snapshots there itself, so refuse before invoking it.
            raise ValueError("The bridge snapshot directory must not be a symlink.")
        try:
            # Fixed argv to the pinned public CLI; never a shell.
            done = self.runner(
                argv,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise BridgeFailure("The Chrome bridge did not answer in time.") from None
        except OSError:
            raise _stop("browser_unavailable") from None
        try:
            envelope = json.loads(done.stdout)
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict):
            raise BridgeFailure("The Chrome bridge returned an unreadable answer.")
        # An error-shaped envelope is an error even when the exit code is zero.
        if envelope.get("isError") or "error" in envelope or done.returncode != 0:
            error = str(envelope.get("error", ""))
            if NOT_OPEN in error:
                raise _stop("connection_required")
            if NOT_OWNED in error:
                raise browser.BrowserStop(
                    "connection_required", "Zunara's Chrome tab is no longer available; stopped."
                )
            raise BridgeFailure("The Chrome bridge reported an error.")
        self._discard_snapshot(envelope, cwd)
        result = envelope.get("result")
        if isinstance(result, str):
            with contextlib.suppress(ValueError):
                return json.loads(result)
        return result

    @staticmethod
    def _discard_snapshot(envelope: dict[str, Any], cwd: Path) -> None:
        """Remove page snapshots the CLI reported writing; they may contain page text.

        The file is named at ``snapshot.file`` (e.g. ``tab-new``) or ``result.snapshot.file``
        (e.g. ``open``/``attach``), and is removed only inside ``.playwright-cli`` here.
        """
        result = envelope.get("result")
        for holder in (envelope, result if isinstance(result, dict) else {}):
            snapshot = holder.get("snapshot")
            name = snapshot.get("file") if isinstance(snapshot, dict) else None
            if not isinstance(name, str) or not re.fullmatch(
                r"\.playwright-cli/[\w.-]+\.yml", name
            ):
                continue
            # The expected folder is built from the resolved cwd, never resolved through itself.
            expected = cwd.resolve() / SNAPSHOTS
            path = expected / Path(name).name
            with contextlib.suppress(OSError):
                inside = not expected.is_symlink() and path.resolve().parent == expected
                if inside and not path.is_symlink() and path.is_file():
                    path.unlink()

    def run(self, spec: dict[str, Any], timeout: float = CALL_TIMEOUT) -> Any:
        program = PROGRAM.replace("__SPEC__", json.dumps(spec, ensure_ascii=True))
        return self.call(["run-code", program], timeout)

    def alive(self) -> bool:
        """True only when the session answers the ping with exactly ``true``."""
        try:
            return self.run({"action": "ping"}, timeout=CALL_TIMEOUT) is True
        except browser.BrowserStop as stop:
            if stop.status == "connection_required":
                return False
            raise


class RemoteLocator:
    """The subset of Playwright's Locator API that PlaywrightInstagram uses, over ``run-code``."""

    def __init__(self, page: "RemotePage", chain: list[dict[str, Any]]) -> None:
        self._page = page
        self._chain = chain

    def _then(self, step: dict[str, Any]) -> "RemoteLocator":
        return RemoteLocator(self._page, [*self._chain, step])

    def locator(self, css: str) -> "RemoteLocator":
        return self._then({"op": "locator", "css": str(css)})

    def get_by_role(
        self, role: str, *, name: object = None, exact: bool | None = None
    ) -> "RemoteLocator":
        if role not in ROLES:
            raise ValueError("Unsupported role.")
        return self._then({"op": "role", "role": role, "name": _js_value(name), "exact": exact})

    def get_by_text(self, text: object) -> "RemoteLocator":
        return self._then({"op": "text", "text": _js_value(text)})

    def filter(self, *, visible: bool | None = None, has_text: object = None) -> "RemoteLocator":
        return self._then({"op": "filter", "visible": visible, "hasText": _js_value(has_text)})

    def nth(self, index: int) -> "RemoteLocator":
        return self._then({"op": "nth", "index": int(index)})

    @property
    def last(self) -> "RemoteLocator":
        return self._then({"op": "last"})

    def _do(self, action: str, timeout: float | None = None, **args: Any) -> Any:
        wait = self._page.step if timeout is None else timeout / 1000
        return self._page._run(action, self._chain, {"timeout": wait * 1000, **args}, wait)

    def count(self) -> int:
        return int(self._do("count"))

    def is_visible(self) -> bool:
        return bool(self._do("isVisible"))

    def is_enabled(self, timeout: float | None = None) -> bool:
        return bool(self._do("isEnabled", timeout))

    def click(self, timeout: float | None = None) -> None:
        self._do("click", timeout)

    def fill(self, value: str, timeout: float | None = None) -> None:
        self._do("fill", timeout, value=str(value))

    def get_attribute(self, name: str, timeout: float | None = None) -> str | None:
        found = self._do("getAttribute", timeout, name=str(name))
        return found if isinstance(found, str) else None

    def input_value(self, timeout: float | None = None) -> str:
        return str(self._do("inputValue", timeout))

    def inner_text(self, timeout: float | None = None) -> str:
        return str(self._do("innerText", timeout))

    def set_input_files(self, path: str, timeout: float | None = None) -> None:
        self._do("setInputFiles", timeout, path=str(Path(path).resolve()))


class RemotePage(RemoteLocator):
    """The Zunara-owned tab. Each operation verifies ownership inside ``run-code``."""

    def __init__(self, cli: Cli, tab: str, step: float = browser.STEP_TIMEOUT) -> None:
        super().__init__(self, [])
        self.cli = cli
        self.tab = tab
        self.step = step

    def _run(
        self, action: str, chain: list[dict[str, Any]], args: dict[str, Any], wait: float
    ) -> Any:
        spec = {"action": action, "tab": self.tab, "chain": chain, "args": args}
        return self.cli.run(spec, timeout=wait + CALL_MARGIN)

    @property
    def url(self) -> str:
        return str(self._run("url", [], {}, self.step))

    def goto(self, url: str, *, wait_until: str = "load", timeout: float | None = None) -> None:
        if urlsplit(url).hostname not in browser.HOSTS or urlsplit(url).scheme != "https":
            raise ValueError("The Chrome bridge only navigates its own tab to Instagram.")
        wait = self.step if timeout is None else timeout / 1000
        args = {"url": url, "waitUntil": wait_until, "timeout": wait * 1000}
        self._run("goto", [], args, wait)

    def wait_for_timeout(self, milliseconds: float) -> None:
        time.sleep(milliseconds / 1000)


Start = Callable[[Cli], None]


def _attach(cli: Cli, token: str | None, timeout: float) -> None:
    """Handshake with the extension. Without a token, the person approves it in Chrome."""
    cli.call(["attach", "--extension=chrome"], timeout, token=token)


@contextlib.contextmanager
def owned_tab(cli: Cli, step: float = browser.STEP_TIMEOUT) -> Iterator[browser.InstagramUI]:
    """Open and claim a new Instagram tab for this run; close only that tab afterwards."""
    tab = secrets.token_hex(12)
    blank = f"about:blank#zunara-{tab}"
    cli.call(["tab-new", blank], CALL_TIMEOUT)  # Output (a tab list) is discarded.
    cli.run({"action": "claim", "tab": tab, "chain": [], "args": {"url": blank}})
    page = RemotePage(cli, tab, step)
    try:
        yield browser.PlaywrightInstagram(page, step_timeout=step)
    finally:
        with contextlib.suppress(Exception):
            page._run("release", [], {}, step)


def connect_chrome(
    config_dir: Path, *, cli: Cli | None = None, start: Start | None = None
) -> browser.Connect:
    """Connector for ``execute``: reuse a live bridge session, re-attach only with a token."""

    @contextlib.contextmanager
    def session(_where: Path, _headed: bool) -> Iterator[browser.InstagramUI]:
        bridge = cli or Cli(config_dir)
        with contextlib.ExitStack() as stack:
            try:
                if start is not None:
                    start(bridge)
                elif not bridge.alive():
                    token = load_token(config_dir)
                    if token is None:
                        # Never prompt or launch a browser from an unattended run.
                        raise _stop("connection_required")
                    _attach(bridge, token, ATTACH_TIMEOUT)
                ui = stack.enter_context(owned_tab(bridge))
            except BridgeFailure:
                # Setup failed before Zunara owned a tab: nothing was attached or shared.
                raise _stop("connection_required") from None
            yield ui

    return session


def connect(
    config_dir: Path,
    username: str,
    timeout: float,
    *,
    cli: Cli | None = None,
    start: Start | None = None,
) -> dict[str, Any]:
    """Interactive handshake plus exact identity check in a Zunara-owned tab."""
    with session_lock(config_dir):
        bridge = cli or Cli(config_dir)
        token = load_token(config_dir)
        try:
            if start is not None:
                start(bridge)
            elif not bridge.alive():
                _attach(bridge, token, timeout)
            with owned_tab(bridge) as ui:
                browser._verify_identity(ui, username, "chrome")
        except browser.BrowserStop as stop:
            detail = str(stop) or browser._message(stop.status, username, "", "chrome")
            raise ValueError(f"Chrome connection stopped ({stop.status}): {detail}") from None
        except BridgeFailure as error:
            raise ValueError(f"Chrome connection stopped: {error}") from None
    return {
        "platform": "instagram",
        "username": username,
        "connection": "chrome",
        "session": bridge.session,
        "status": "connected",
        "reconnect": (
            "automatic with the configured extension token"
            if token
            else "needs your approval in Chrome; unattended runs stop if the bridge disconnects"
        ),
        "note": "Chrome must keep running; the Instagram session can still expire.",
    }


def disconnect(config_dir: Path, *, cli: Cli | None = None) -> dict[str, Any]:
    """Detach the bridge session; Chrome and its tabs keep running."""
    with session_lock(config_dir):
        bridge = cli or Cli(config_dir)
        try:
            bridge.call(["detach"], CALL_TIMEOUT)
        except browser.BrowserStop as stop:
            if stop.status != "connection_required":
                raise ValueError(f"Chrome bridge unavailable ({stop.status}).") from None
        except BridgeFailure as error:
            raise ValueError(str(error)) from None
    return {"connection": "chrome", "session": bridge.session, "status": "detached"}


def doctor(config_dir: Path) -> dict[str, Any]:
    """Offline report: runs no CLI command and reads no token."""
    return {
        "cli_package": CLI_PACKAGE,
        "npx_installed": shutil.which("npx") is not None,
        "session": SESSION,
        **token_status(config_dir),
        "bridge_connection": "not verified; doctor runs no CLI command",
    }
