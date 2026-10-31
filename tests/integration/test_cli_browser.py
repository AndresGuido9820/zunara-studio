"""Installed CLI surface of `zunara browser`. Opens no browser and touches no account."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_installed_cli_offers_browser_and_doctor_stays_offline(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "ZUNARA_CONFIG_DIR": str(home / "config"),
        "ZUNARA_DATA_DIR": str(home / "data"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = Path(sys.executable).with_name("zunara")
    command = [str(script)] if script.exists() else [sys.executable, "-m", "zunara"]

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, *args], capture_output=True, text=True, env=env, cwd=home, timeout=60
        )

    shown = run("browser", "--help")
    assert shown.returncode == 0
    assert all(
        n in shown.stdout for n in ("doctor", "login", "connect", "disconnect", "publish", "status")
    )
    doctor = run("browser", "doctor")
    assert doctor.returncode == 0, doctor.stderr
    report = json.loads(doctor.stdout)
    assert report["platforms"] == ["instagram"] and report["profiles"] == []
    assert report["live_session"].startswith("unknown")
    assert report["chrome_bridge"]["token_source"] is None
    refused = run("browser", "login", "--platform", "tiktok", "--username", "someone")
    assert refused.returncode == 2 and "only instagram" in refused.stderr
    assert not (home / "config").exists()
