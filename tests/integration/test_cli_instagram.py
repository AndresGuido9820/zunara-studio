"""Installed CLI surface of `zunara instagram`. No network, token or upload."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_installed_cli_offers_instagram_and_doctor_stays_offline(tmp_path: Path) -> None:
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

    shown = run("instagram", "--help")
    assert shown.returncode == 0
    assert all(n in shown.stdout for n in ("doctor", "account", "publish", "status", "finish"))
    doctor = run("instagram", "doctor")
    assert doctor.returncode == 0, doctor.stderr
    report = json.loads(doctor.stdout)
    assert report["token_source"] is None
    assert report["token_file"] == str(home / "config" / "instagram-access-token")
    assert report["live_auth"].startswith("unknown")
    assert list(home.iterdir()) == []
