"""Real Chromium driving local fixture pages through the production Playwright adapter.

Requests to www.instagram.com are fulfilled from the fixture below by a test-only route; every
other request is aborted, so nothing reaches the Internet or a real account. Skipped when the
``browser`` extra or a Chromium build is unavailable.
"""

import contextlib
import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from zunara import browser, instagram, queue

pytestmark = [pytest.mark.integration, pytest.mark.browser]
sync_api = pytest.importorskip("playwright.sync_api")

USER = "studio.clips"
PROBE = {
    "duration_s": 12.0,
    "width": 1080,
    "height": 1920,
    "codec": "h264",
    "has_audio": True,
    "audio_codec": "aac",
    "audio_sample_rate": 48000,
}

FIXTURE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Fixture</title></head><body>
<main id="app"></main>
<div id="dialog" role="dialog" hidden></div>
<script>
const S = __SCENARIO__;
const app = document.getElementById("app");
const dialog = document.getElementById("dialog");
const report = (kind, body) =>
  fetch("/__fixture/" + kind, {method: "POST", body: JSON.stringify(body || {})});
let aspect = "square";

function nav(user) {
  app.innerHTML = `<nav>
    <a href="/${user}/"><img alt="Foto del perfil de ${user}" src="data:,"> Perfil</a>
    <a href="/${user}/"><span><img alt="Foto del perfil de ${user}" src="data:,"></span>
      <span>Perfil</span></a>
    <a href="#" id="create"><span>Nueva publicación</span> <span>Crear</span></a>
    <div id="menu" hidden><a href="#" id="post">Publicación Publicación</a></div>
  </nav>`;
  document.getElementById("create").onclick = (e) => {
    e.preventDefault(); document.getElementById("menu").hidden = false; };
  document.getElementById("post").onclick = (e) => { e.preventDefault(); start(); };
}

function start() {
  dialog.hidden = false;
  dialog.innerHTML = `<h1>Crear nueva publicación</h1>
    <button>Seleccionar del ordenador</button>
    <input type="file" accept="video/mp4" style="display:none">`;
  dialog.querySelector("input").onchange = (e) => {
    const f = e.target.files[0];
    report("attached", {name: f.name, size: f.size});
    crop();
  };
}

function crop() {
  dialog.innerHTML = `<h1>Recortar</h1>
    <button aria-label="Seleccionar recorte">[]</button>
    <div id="crops" hidden><button id="orig" aria-pressed="false">Original</button>
      <button aria-pressed="true">1:1</button></div>
    <button id="next1">Siguiente</button>
    ${S.reelInfo ? `<div role="dialog" id="info">
      <p>Ahora las publicaciones de vídeo se comparten como reels</p>
      <button id="ok">Aceptar</button></div>` : ""}`;
  if (S.reelInfo) document.getElementById("ok").onclick = () =>
    document.getElementById("info").remove();
  dialog.querySelector('[aria-label="Seleccionar recorte"]').onclick = () =>
    document.getElementById("crops").hidden = false;
  document.getElementById("orig").onclick = (e) => {
    if (!S.cropStuck) { e.target.setAttribute("aria-pressed", "true"); aspect = "original"; }
  };
  document.getElementById("next1").onclick = edit;
}

function edit() {
  dialog.innerHTML = `<h1>Editar</h1><button id="next2">Siguiente</button>`;
  document.getElementById("next2").onclick = caption;
}

function caption() {
  dialog.innerHTML = `<h1>Crear nuevo reel</h1>
    ${S.textarea ? `<textarea aria-label="Escribe un pie de foto..."></textarea>`
      : `<div role="textbox" contenteditable="true" aria-label="Escribe un pie de foto..."></div>`}
    <button id="share">Compartir</button>`;
  document.getElementById("share").onclick = () => {
    const box = dialog.querySelector('[aria-label="Escribe un pie de foto..."]');
    const text = S.textarea ? box.value : box.innerText;
    report("shared", {caption: text, aspect});
    if (S.outcome === "success") setTimeout(() => {
      dialog.innerHTML = `<h1>Se ha compartido el reel</h1>
        <a href="/reel/Fixture${Math.floor(Math.random() * 1e6)}/">Ver</a>`;
    }, 300);
  };
}

if (location.pathname.startsWith("/accounts/login")) {
  app.innerHTML = `<form><input name="username"><input name="password" type="password"></form>`;
  if (S.loginAfterMs) setTimeout(() => { history.replaceState(null, "", "/"); nav(S.user); },
                                 S.loginAfterMs);
} else if (S.page === "login") {
  app.innerHTML = `<form><input name="username"><input name="password" type="password"></form>`;
} else if (S.page === "captcha") {
  app.innerHTML = `<iframe title="captcha challenge" src="about:blank"></iframe>`;
} else {
  nav(S.user);
}
</script></body></html>"""


@pytest.fixture(scope="module")
def chromium_ok(tmp_path_factory: pytest.TempPathFactory) -> None:
    try:
        with sync_api.sync_playwright() as driver:
            driver.chromium.launch().close()
    except Exception:
        pytest.skip("Chromium for Playwright is not installed")


class Site:
    """Test-only route: serves the fixture for Instagram URLs and aborts everything else."""

    def __init__(self, **scenario: Any) -> None:
        self.scenario = {"user": USER, "outcome": "success", "page": "home", **scenario}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.blocked: list[str] = []

    def prepare(self, context: Any) -> None:
        context.route("**/*", self.handle)

    def handle(self, route: Any) -> None:
        url = urlsplit(route.request.url)
        if url.scheme == "data":
            route.continue_()
        elif url.hostname != "www.instagram.com":
            self.blocked.append(url.hostname or "")
            route.abort()
        elif url.path.startswith("/__fixture/"):
            body = json.loads(route.request.post_data or "{}")
            self.events.append((url.path.rsplit("/", 1)[1], body))
            route.fulfill(status=204)
        elif url.path == "/favicon.ico":
            route.fulfill(status=404)
        else:
            html = FIXTURE.replace("__SCENARIO__", json.dumps(self.scenario))
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    def connect(self, profile: Path, headed: bool) -> contextlib.AbstractContextManager[Any]:
        @contextlib.contextmanager
        def session() -> Iterator[Any]:
            with browser.playwright_page(profile, False, prepare=self.prepare) as page:
                yield browser.PlaywrightInstagram(page, step_timeout=5)

        return session()

    def kinds(self, kind: str) -> list[dict[str, Any]]:
        return [body for name, body in self.events if name == kind]


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chromium_ok: None) -> tuple[Path, Path]:
    monkeypatch.setattr(instagram, "probe", lambda _path: dict(PROBE))
    work = tmp_path / "session-01"
    (work / "out").mkdir(parents=True)
    for stem in ("clip-01", "clip-02"):
        (work / "out" / f"{stem}.mp4").write_bytes(f"synthetic {stem}".encode())
    queue.refresh(work)
    rows = queue.load(work)
    for row in rows:
        row.update(title="Title", description=f"Caption for {row['file']} #fixture")
    queue.save(work, rows)
    queue.approve(work, {"clip-01", "clip-02"})
    config = tmp_path / "config"
    browser.profile_dir(config, "instagram", USER, create=True)
    return work, config


def execute(work: Path, config: Path, site: Site, only: set[str], **kwargs: Any) -> Any:
    items = browser.plan(work, only, USER, retry_unshared=False)
    return browser.execute(
        work,
        config,
        items,
        username=USER,
        headed=False,
        share_timeout=kwargs.get("share_timeout", 10),
        connect=site.connect,
    )


def test_full_flow_publishes_two_clips_and_keeps_profile_private(setup: tuple[Path, Path]):
    work, config = setup
    site = Site(reelInfo=True)
    result = execute(work, config, site, {"clip-01", "clip-02"})
    assert [r["state"] for r in result["records"]] == ["published", "published"]
    assert all(
        r["permalink"].startswith("https://www.instagram.com/reel/") for r in result["records"]
    )
    attached = site.kinds("attached")
    assert [a["size"] for a in attached] == [len(b"synthetic clip-01"), len(b"synthetic clip-02")]
    assert [s["caption"] for s in site.kinds("shared")] == [
        "Caption for clip-01.mp4 #fixture",
        "Caption for clip-02.mp4 #fixture",
    ]
    assert {s["aspect"] for s in site.kinds("shared")} == {"original"}
    assert site.blocked == []
    profile = config / browser.PROFILE_ROOT / "instagram" / USER
    assert not stat.S_IMODE(profile.stat().st_mode) & 0o077
    assert not any(work.rglob("Cookies"))
    again = Site()
    assert execute(work, config, again, {"clip-01", "clip-02"})["mode"] == "nothing-to-do"
    assert again.events == []


def test_textarea_caption_is_verified_by_value(setup: tuple[Path, Path]):
    work, config = setup
    site = Site(textarea=True)
    result = execute(work, config, site, {"clip-01"})
    assert result["records"][0]["state"] == "published"
    assert site.kinds("shared")[0]["caption"] == "Caption for clip-01.mp4 #fixture"


def test_success_words_in_caption_are_not_proof_of_publication(setup: tuple[Path, Path]):
    work, config = setup
    rows = queue.load(work)
    rows[0]["description"] = "Se ha compartido el reel. Your reel has been shared"
    queue.save(work, rows)
    site = Site(outcome="hang")
    with pytest.raises(ValueError, match="will not be retried"):
        execute(work, config, site, {"clip-01"}, share_timeout=2)
    assert site.kinds("shared")[0]["caption"].startswith("Se ha compartido el reel")
    record = browser.load_ledger(work)["records"][0]
    assert (record["state"], record["observed"]) == ("unknown", "timeout")


def test_account_mismatch_attaches_nothing(setup: tuple[Path, Path]):
    work, config = setup
    site = Site(user="someone.else")
    with pytest.raises(ValueError, match="account_mismatch"):
        execute(work, config, site, {"clip-01"})
    assert site.events == []


@pytest.mark.parametrize(
    ("page", "status"), [("login", "login_required"), ("captcha", "checkpoint_required")]
)
def test_login_or_captcha_stops_before_upload(setup: tuple[Path, Path], page: str, status: str):
    work, config = setup
    site = Site(page=page)
    with pytest.raises(ValueError, match=status):
        execute(work, config, site, {"clip-01"})
    assert site.events == []
    assert browser.load_ledger(work)["records"][0]["media_selected"] is False


def test_crop_not_preserved_stops_before_share(setup: tuple[Path, Path]):
    work, config = setup
    site = Site(cropStuck=True)
    with pytest.raises(ValueError, match="ui_changed"):
        execute(work, config, site, {"clip-01"})
    assert len(site.kinds("attached")) == 1 and site.kinds("shared") == []
    record = browser.load_ledger(work)["records"][0]
    assert (record["state"], record["media_selected"], record["share_clicked"]) == (
        "stopped",
        True,
        False,
    )


def test_unconfirmed_share_stays_unknown_and_blocks_retry(setup: tuple[Path, Path]):
    work, config = setup
    site = Site(outcome="hang")
    with pytest.raises(ValueError, match="will not be retried"):
        execute(work, config, site, {"clip-01"}, share_timeout=2)
    assert len(site.kinds("shared")) == 1
    assert browser.load_ledger(work)["records"][0]["state"] == "unknown"
    retry = Site()
    with pytest.raises(ValueError, match="unresolved"):
        execute(work, config, retry, {"clip-01"})
    assert retry.events == []


def test_login_waits_for_the_person_then_verifies_identity(tmp_path: Path, chromium_ok: None):
    config = tmp_path / "config"
    site = Site(loginAfterMs=1500)
    result = browser.login(config, USER, 30, connect=site.connect)
    assert result["status"] == "connected"
    wrong = Site(loginAfterMs=500, user="someone.else")
    with pytest.raises(ValueError, match="account_mismatch"):
        browser.login(config, USER, 30, connect=wrong.connect)
