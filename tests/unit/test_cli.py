"""The CLI boundary must not expose remote exception payloads."""

import pytest

from zunara import cli


def test_sdk_failure_is_sanitized(monkeypatch, capsys):
    def fail(args):
        raise RuntimeError("SYNTHETIC_PRIVATE_PAYLOAD")

    monkeypatch.setattr(cli, "run", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["doctor"])
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "SYNTHETIC_PRIVATE_PAYLOAD" not in output
    assert "Traceback" not in output
    assert "operation failed" in output


def test_doctor_is_json_and_has_no_side_effects(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("ZUNARA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ZUNARA_DATA_DIR", str(tmp_path / "data"))
    cli.main(["doctor"])
    assert json.loads(capsys.readouterr().out)["config_dir"] == str(tmp_path / "config")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("selection", ["", ",", " , "])
def test_empty_publish_selector_is_rejected_before_auth(selection, monkeypatch, capsys):
    def unexpected_client(*args, **kwargs):
        pytest.fail("Empty selection must not authorize a channel")

    monkeypatch.setattr(cli.youtube, "client", unexpected_client)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "publish",
                "unused-workspace",
                "--channel",
                "main",
                "--channel-id",
                "UCtest",
                "--only",
                selection,
                "--execute",
            ]
        )
    assert error.value.code == 2
    assert "--only must name" in capsys.readouterr().err
