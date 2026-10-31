"""Main-Chrome bridge: argv, JSON envelopes, regex translation, token handling and ownership.

A fake runner stands in for the Playwright CLI; nothing runs Node, Chrome or a network call.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from zunara import browser, browser_bridge

TOKEN = "synthetic-bridge-token-0123456789"  # gitleaks:allow -- synthetic test value


class Runner:
    """Records each CLI invocation and replies from a scripted list of envelopes."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        reply = self.replies.pop(0) if self.replies else {"result": "true"}
        if isinstance(reply, BaseException):
            raise reply
        code = 1 if isinstance(reply, dict) and reply.get("isError") else 0
        stdout = reply if isinstance(reply, str) else json.dumps(reply)
        return subprocess.CompletedProcess(argv, code, stdout, "stderr " + TOKEN)

    def actions(self) -> list[str]:
        found = []
        for call in self.calls:
            args = call["argv"][call["argv"].index("--json") + 1 :]
            if args[0] == "run-code":
                spec = json.loads(args[1].split("const spec = ", 1)[1].split(";\n", 1)[0])
                found.append(spec["action"])
            else:
                found.append(args[0])
        return found


def make(tmp_path: Path, *replies: Any) -> tuple[browser_bridge.Cli, Runner]:
    runner = Runner(*replies)
    cli = browser_bridge.Cli(tmp_path / "config", runner=runner, command=["playwright-cli"])
    return cli, runner


def test_selector_regexes_translate_to_javascript():
    patterns = [v for v in vars(browser).values() if isinstance(v, re.Pattern)]
    assert len(patterns) > 10
    for pattern in patterns:
        if pattern in (browser.SHA256, browser.PROFILE_HREF):
            continue
        translated = browser_bridge.js_regex(pattern)
        assert translated["re"] == pattern.pattern
        assert translated["flags"] in {"", "i"}
    assert browser_bridge.js_regex(browser.SUCCESS)["flags"] == "i"


@pytest.mark.parametrize(
    "pattern",
    [re.compile(r"(?P<name>x)"), re.compile(r"\Ax"), re.compile("x", re.MULTILINE)],
)
def test_python_only_regex_is_refused(pattern: re.Pattern[str]):
    with pytest.raises(ValueError):
        browser_bridge.js_regex(pattern)


def test_default_command_is_pinned_npx_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    npx = tmp_path / "npx"
    npx.write_text("#!/bin/sh\n")
    monkeypatch.setattr(browser_bridge.shutil, "which", lambda _name: str(npx))
    runner = Runner({"result": "true"})
    browser_bridge.Cli(tmp_path, runner=runner).call(["detach"], 5)
    argv = runner.calls[0]["argv"]
    assert argv[:5] == [str(npx), "--yes", "--package", "@playwright/cli@0.1.22",
                        "playwright-cli"]  # fmt: skip
    assert argv[5:] == ["-s=zunara-instagram", "--json", "detach"]
    assert runner.calls[0]["timeout"] == 5 and "shell" not in runner.calls[0]


@pytest.mark.parametrize("case", ["missing", "cmd-shim", "windows"])
def test_unsupported_npx_is_browser_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
):
    shim = tmp_path / "npx.cmd"
    shim.write_text("@echo off\n")
    real = tmp_path / "npx"
    real.write_text("#!/bin/sh\n")
    found = {"missing": None, "cmd-shim": str(shim), "windows": str(real)}[case]
    monkeypatch.setattr(browser_bridge.shutil, "which", lambda _name: found)
    if case == "windows":
        monkeypatch.setattr(browser_bridge.os, "name", "nt")
    runner = Runner()
    with pytest.raises(browser.BrowserStop) as caught:
        browser_bridge.Cli(tmp_path, runner=runner).call(["run-code", "caption"], 5)
    assert caught.value.status == "browser_unavailable" and runner.calls == []


def test_error_envelope_with_zero_exit_is_an_error(tmp_path: Path):
    runner = Runner()
    runner.replies = []
    cli = browser_bridge.Cli(tmp_path / "config", runner=runner, command=["playwright-cli"])

    def zero(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        body = json.dumps({"error": "Error: something " + TOKEN})
        return subprocess.CompletedProcess(argv, 0, body, "")

    cli.runner = zero  # type: ignore[assignment]
    with pytest.raises(browser_bridge.BridgeFailure):
        cli.call(["run-code", "x"], 5)
    with pytest.raises(browser_bridge.BridgeFailure):
        cli.alive()


@pytest.mark.parametrize("reply", [{"result": "false"}, {"result": "1"}, {"result": '"ok"'}])
def test_alive_requires_exact_true(tmp_path: Path, reply: dict[str, str]):
    cli, _ = make(tmp_path, reply)
    assert cli.alive() is False


def test_token_only_reaches_the_attach_subprocess(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(browser_bridge.TOKEN_ENV, TOKEN)
    cli, runner = make(tmp_path, {"result": "ok"}, {"result": "ok"})
    cli.call(["attach", "--extension=chrome"], 5, token=TOKEN)
    cli.call(["tab-new", browser.HOME], 5)
    first, second = runner.calls
    assert first["env"][browser_bridge.TOKEN_ENV] == TOKEN
    assert browser_bridge.TOKEN_ENV not in second["env"]
    assert all(TOKEN not in arg for call in runner.calls for arg in call["argv"])
    cwd = Path(first["cwd"])
    assert cwd == tmp_path / "config" / browser_bridge.BRIDGE_DIR
    assert stat.S_IMODE(cwd.stat().st_mode) == 0o700


def test_nested_json_result_is_decoded(tmp_path: Path):
    cli, _ = make(tmp_path, {"result": '{"url":"about:blank","n":1}'}, {"result": "not json"})
    assert cli.call(["run-code", "x"], 5) == {"url": "about:blank", "n": 1}
    assert cli.call(["run-code", "x"], 5) == "not json"


@pytest.mark.parametrize(
    ("reply", "kind", "status"),
    [
        ({"isError": True, "error": f"Error: boom https://x.test/?t={TOKEN}"}, "failure", None),
        ("<html>not json " + TOKEN, "failure", None),
        (subprocess.TimeoutExpired(["x"], 5), "failure", None),
        (
            {"isError": True, "error": "The browser 'zunara-instagram' is not open"},
            "stop",
            "connection_required",
        ),
        ({"isError": True, "error": "Error: zunara-tab-not-owned"}, "stop", "connection_required"),
    ],  # fmt: skip
)
def test_errors_are_sanitized(tmp_path: Path, reply: Any, kind: str, status: str | None):
    cli, _ = make(tmp_path, reply)
    expected = browser_bridge.BridgeFailure if kind == "failure" else browser.BrowserStop
    with pytest.raises(expected) as caught:
        cli.call(["run-code", "x"], 5)
    assert TOKEN not in str(caught.value) and "https://" not in str(caught.value)
    if status:
        assert caught.value.status == status  # type: ignore[attr-defined]


def test_reported_snapshot_inside_private_dir_is_removed(tmp_path: Path):
    cli, _ = make(tmp_path)
    folder = browser_bridge.bridge_dir(tmp_path / "config", create=True) / ".playwright-cli"
    folder.mkdir()
    (folder / "page.yml").write_text("page text")
    outside = tmp_path / "outside.yml"
    outside.write_text("keep")
    (folder / "open.yml").write_text("page text")
    cli.runner = Runner(  # type: ignore[assignment]
        {"result": "", "snapshot": {"file": ".playwright-cli/page.yml"}},
        {"session": "s", "result": {"snapshot": {"file": ".playwright-cli/open.yml"}}},
        {"result": "", "snapshot": {"file": "../../outside.yml"}},
        {"result": {"snapshot": {"file": ".playwright-cli/../../../outside.yml"}}},
    )
    for _ in range(4):
        cli.call(["tab-new", "about:blank"], 5)
    assert not (folder / "page.yml").exists() and not (folder / "open.yml").exists()
    assert outside.exists()


def test_symlinked_snapshot_dir_is_refused_and_victim_kept(tmp_path: Path):
    cli, runner = make(tmp_path)
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "page.yml"
    victim.write_text("keep me")
    cwd = browser_bridge.bridge_dir(tmp_path / "config", create=True)
    (cwd / ".playwright-cli").symlink_to(victim_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        cli.call(["tab-new", "about:blank"], 5)
    assert runner.calls == []  # The CLI never ran, so it wrote nothing through the link.
    browser_bridge.Cli._discard_snapshot(
        {"snapshot": {"file": ".playwright-cli/page.yml"}}, cwd
    )  # Cleanup alone must not follow the link either.
    assert victim.read_text() == "keep me"


def test_program_embeds_values_only_as_json(tmp_path: Path):
    cli, runner = make(tmp_path, {"result": "null"})
    page = browser_bridge.RemotePage(cli, "tab1", step=1)
    caption = 'Hi "there" `${alert(1)}` </script>   #tag'
    page.get_by_role("textbox", name=browser.CAPTION_BOX).fill(caption)
    program = runner.calls[0]["argv"][-1]
    spec = json.loads(program.split("const spec = ", 1)[1].split(";\n", 1)[0])
    assert program.replace(json.dumps(spec, ensure_ascii=True), "__SPEC__") == (
        browser_bridge.PROGRAM
    )
    assert spec["args"]["value"] == caption and spec["tab"] == "tab1"
    assert spec["chain"] == [
        {
            "op": "role",
            "role": "textbox",
            "name": {"re": browser.CAPTION_BOX.pattern, "flags": ""},
            "exact": None,
        }
    ]


def test_remote_page_only_navigates_to_instagram(tmp_path: Path):
    cli, runner = make(tmp_path)
    page = browser_bridge.RemotePage(cli, "tab1", step=1)
    for url in ("https://evil.test/", "http://www.instagram.com/", "file:///etc/passwd"):
        with pytest.raises(ValueError, match="only navigates"):
            page.goto(url)
    assert runner.calls == []
    with pytest.raises(ValueError, match="role"):
        page.get_by_role("checkbox")


def test_owned_tab_claims_new_tab_and_releases_only_it(tmp_path: Path):
    cli, runner = make(tmp_path)
    with browser_bridge.owned_tab(cli, step=1) as ui:
        assert isinstance(ui, browser.PlaywrightInstagram)
    assert runner.actions() == ["tab-new", "claim", "release"]
    blank = runner.calls[0]["argv"][-1]
    assert re.fullmatch(r"about:blank#zunara-[0-9a-f]{24}", blank)
    claim = json.loads(runner.calls[1]["argv"][-1].split("const spec = ", 1)[1].split(";\n")[0])
    assert claim["args"]["url"] == blank and blank.endswith(claim["tab"])
    assert "page.url() !== spec.args.url" in browser_bridge.PROGRAM


def test_failed_claim_stops_without_releasing_any_tab(tmp_path: Path):
    wrong = {"isError": True, "error": "Error: zunara-tab-not-owned"}
    cli, runner = make(tmp_path, {"result": ""}, wrong)
    opener = browser_bridge.connect_chrome(tmp_path / "config", cli=cli, start=lambda _cli: None)
    with pytest.raises(browser.BrowserStop) as caught, opener(tmp_path, False):
        pass
    assert caught.value.status == "connection_required"
    assert runner.actions() == ["tab-new", "claim"]  # No release: nothing was owned.


def test_bridge_failure_during_setup_becomes_connection_required(tmp_path: Path):
    cli, runner = make(tmp_path, {"result": ""}, "garbage " + TOKEN)
    opener = browser_bridge.connect_chrome(tmp_path / "config", cli=cli, start=lambda _cli: None)
    with pytest.raises(browser.BrowserStop) as caught, opener(tmp_path, False):
        pass
    assert caught.value.status == "connection_required" and TOKEN not in str(caught.value)


def test_unattended_run_without_bridge_or_token_stops(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(browser_bridge.TOKEN_ENV, raising=False)
    not_open = {"isError": True, "error": "The browser 'zunara-instagram' is not open"}
    cli, runner = make(tmp_path, not_open)
    opener = browser_bridge.connect_chrome(tmp_path / "config", cli=cli)
    with pytest.raises(browser.BrowserStop) as caught, opener(tmp_path, False):
        pass
    assert caught.value.status == "connection_required"
    assert runner.actions() == ["ping"]  # Never attach, open or launch a browser.
    message = browser._message("connection_required", "studio.clips", "", "chrome")
    assert "browser connect" in message and "browser login" not in message
    assert "{username}" not in message


def test_unattended_reattach_uses_configured_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(browser_bridge.TOKEN_ENV, TOKEN)
    not_open = {"isError": True, "error": "The browser 'zunara-instagram' is not open"}
    cli, runner = make(tmp_path, not_open)
    opener = browser_bridge.connect_chrome(tmp_path / "config", cli=cli)
    with opener(tmp_path, False):
        pass
    assert runner.actions() == ["ping", "attach", "tab-new", "claim", "release"]
    assert runner.calls[2]["argv"][-1].startswith("about:blank#zunara-")
    assert runner.calls[1]["argv"][-1] == "--extension=chrome"
    assert runner.calls[1]["env"][browser_bridge.TOKEN_ENV] == TOKEN
    assert "open" not in runner.actions() and "close" not in runner.actions()


def test_disconnect_only_detaches(tmp_path: Path):
    cli, runner = make(tmp_path)
    result = browser_bridge.disconnect(tmp_path / "config", cli=cli)
    assert result["status"] == "detached" and runner.actions() == ["detach"]


def test_token_sources_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(browser_bridge.TOKEN_ENV, raising=False)
    config = tmp_path / "config"
    config.mkdir()
    assert browser_bridge.load_token(config) is None
    path = config / browser_bridge.TOKEN_FILE
    path.write_text(TOKEN)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        browser_bridge.load_token(config)
    path.chmod(0o600)
    assert browser_bridge.load_token(config) == TOKEN
    status = browser_bridge.token_status(config)
    assert status["token_source"] == "file" and status["token_file_private"] is True
    assert TOKEN not in json.dumps(browser_bridge.doctor(config))
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        browser_bridge.load_token(config)
    monkeypatch.setenv(browser_bridge.TOKEN_ENV, "short")
    with pytest.raises(ValueError, match="malformed"):
        browser_bridge.load_token(config)


def test_session_lock_is_exclusive(tmp_path: Path):
    with (
        browser_bridge.session_lock(tmp_path),
        pytest.raises(ValueError, match="in use"),
        browser_bridge.session_lock(tmp_path),
    ):
        pass


def test_bridge_dir_rejects_symlink_and_loose_permissions(tmp_path: Path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / browser_bridge.BRIDGE_DIR).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        browser_bridge.bridge_dir(tmp_path, create=True)
    other = tmp_path / "other"
    (other / browser_bridge.BRIDGE_DIR).mkdir(parents=True, mode=0o755)
    os.chmod(other / browser_bridge.BRIDGE_DIR, 0o755)
    with pytest.raises(ValueError, match="private"):
        browser_bridge.bridge_dir(other, create=False)


@pytest.mark.parametrize("session", ["Bad", "x", "has space", "-lead"])
def test_session_names_are_restricted(tmp_path: Path, session: str):
    with pytest.raises(ValueError, match="session"):
        browser_bridge.Cli(tmp_path, session)


def test_plaintext_missing_extension_error_is_actionable_and_sanitized(tmp_path: Path):
    def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, "", "Error: Playwright Extension not found in /private/example " + TOKEN
        )

    cli = browser_bridge.Cli(tmp_path, runner=missing, command=["playwright-cli"])
    with pytest.raises(browser.BrowserStop) as caught:
        cli.call(["attach", "--extension=chrome"], 5)
    assert caught.value.status == "connection_required"
    assert "Install it in your main Chrome" in str(caught.value)
    assert TOKEN not in str(caught.value) and "/private/example" not in str(caught.value)
