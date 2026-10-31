"""The real pinned Playwright CLI driving an isolated headless browser through the bridge code.

Opt-in: set ZUNARA_TEST_PLAYWRIGHT_CLI=1 (needs Node.js/npx and the CLI package, which npx may
download once). Each test opens its own uniquely named, headless CLI session through a test-only
start hook, serves the local Instagram fixture by a route installed from the test, aborts every
other request and closes that session afterwards. Nothing attaches to a real Chrome or account.
"""

import contextlib
import importlib.util
import json
import os
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from zunara import browser, browser_bridge, instagram, queue

pytestmark = [pytest.mark.integration, pytest.mark.browser]
if os.environ.get("ZUNARA_TEST_PLAYWRIGHT_CLI") != "1" or shutil.which("npx") is None:
    pytest.skip("set ZUNARA_TEST_PLAYWRIGHT_CLI=1 with npx available", allow_module_level=True)

_spec = importlib.util.spec_from_file_location(
    "zunara_browser_fixture", Path(__file__).with_name("test_browser_playwright.py")
)
assert _spec and _spec.loader
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
USER = _fixture.USER

# Test-only: route Instagram URLs to the fixture, abort everything else, record fixture events.
ROUTE = """async (page) => {
  const html = __HTML__;
  const context = page.context();
  context.__events = [];
  context.__blocked = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.protocol === "data:" || url.protocol === "about:") return route.continue();
    if (url.hostname !== "www.instagram.com") { context.__blocked.push(url.hostname); return route.abort(); }
    if (url.pathname.startsWith("/__fixture/")) {
      context.__events.push([url.pathname.split("/").pop(), JSON.parse(route.request().postData() || "{}")]);
      return route.fulfill({status: 204});
    }
    if (url.pathname === "/favicon.ico") return route.fulfill({status: 404});
    return route.fulfill({status: 200, contentType: "text/html; charset=utf-8", body: html});
  });
  return true;
}"""


class Isolated:
    def __init__(self, config: Path, **scenario: Any) -> None:
        self.cli = browser_bridge.Cli(config, f"zunara-test-{secrets.token_hex(4)}")
        self.scenario = {"user": USER, "outcome": "success", "page": "home", **scenario}

    def start(self, cli: browser_bridge.Cli) -> None:
        cli.call(["open", "about:blank"], 120)  # Test-only isolated headless browser.
        html = _fixture.FIXTURE.replace("__SCENARIO__", json.dumps(self.scenario))
        code = ROUTE.replace("__HTML__", json.dumps(html))
        cli.call(["run-code", code], 60)

    def probe(self, expression: str) -> Any:
        return self.cli.call(["run-code", f"async (page) => {expression}"], 60)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [b for k, b in self.probe("page.context().__events") if k == kind]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.cli.call(["close"], 60)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    monkeypatch.setattr(instagram, "probe", lambda _path: dict(_fixture.PROBE))
    monkeypatch.delenv(browser_bridge.TOKEN_ENV, raising=False)
    work = tmp_path / "session-01"
    (work / "out").mkdir(parents=True)
    (work / "out" / "clip-01.mp4").write_bytes(b"synthetic clip-01")
    queue.refresh(work)
    rows = queue.load(work)
    rows[0].update(title="Title", description="Bridge caption #fixture")
    queue.save(work, rows)
    queue.approve(work, {"clip-01"})
    yield work, tmp_path / "config"


def publish(work: Path, config: Path, site: Isolated) -> Any:
    items = browser.plan(work, {"clip-01"}, USER, retry_unshared=False)
    return browser.execute(
        work,
        config,
        items,
        username=USER,
        headed=False,
        share_timeout=30,
        connection="chrome",
        connect=browser_bridge.connect_chrome(config, cli=site.cli, start=site.start),
    )


def test_real_cli_publishes_through_owned_tab(setup: tuple[Path, Path]):
    work, config = setup
    site = Isolated(config, reelInfo=True)
    try:
        result = publish(work, config, site)
        [record] = result["records"]
        assert (record["state"], record["connection"]) == ("published", "chrome")
        assert [a["size"] for a in site.events("attached")] == [len(b"synthetic clip-01")]
        assert site.events("shared")[0] == {
            "caption": "Bridge caption #fixture",
            "aspect": "original",
        }
        assert site.probe("page.context().__blocked") == []
        # Only Zunara's tab was closed; the session's original page is still open.
        assert site.probe("page.context().pages().length") == 1
        assert not any((config / browser_bridge.BRIDGE_DIR / ".playwright-cli").glob("*.yml"))
    finally:
        site.close()


def test_real_cli_never_claims_or_acts_on_another_tab(setup: tuple[Path, Path]):
    _work, config = setup
    site = Isolated(config)
    try:
        site.start(site.cli)
        site.probe('{ await page.goto("https://www.instagram.com/"); return true; }')
        claim = {
            "action": "claim",
            "tab": "t1",
            "chain": [],
            "args": {"url": "about:blank#zunara-t1"},
        }
        with pytest.raises(browser.BrowserStop) as caught:
            site.cli.run(claim)  # The current page is a real (fixture) Instagram tab.
        assert caught.value.status == "connection_required"
        with browser_bridge.owned_tab(site.cli, step=5) as ui:
            page = ui.page  # type: ignore[attr-defined]
            assert page.url.startswith("about:blank#zunara-")
        with pytest.raises(browser.BrowserStop):
            page.url  # noqa: B018 -- the released tab's marker check must fail
        assert site.probe("page.url()") == "https://www.instagram.com/"
    finally:
        site.close()


def test_real_cli_account_mismatch_attaches_nothing(setup: tuple[Path, Path]):
    work, config = setup
    site = Isolated(config, user="someone.else")
    try:
        with pytest.raises(ValueError, match="account_mismatch.*main Chrome"):
            publish(work, config, site)
        assert site.events("attached") == []
    finally:
        site.close()


def test_missing_bridge_session_stops_without_opening_a_browser(setup: tuple[Path, Path]):
    work, config = setup
    cli = browser_bridge.Cli(config, f"zunara-test-{secrets.token_hex(4)}")
    items = browser.plan(work, {"clip-01"}, USER, retry_unshared=False)
    with pytest.raises(ValueError, match="connection_required"):
        browser.execute(
            work, config, items, username=USER, headed=False, share_timeout=30,
            connection="chrome", connect=browser_bridge.connect_chrome(config, cli=cli),
        )  # fmt: skip
    assert cli.alive() is False
