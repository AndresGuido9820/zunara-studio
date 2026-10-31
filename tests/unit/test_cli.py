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


@pytest.mark.parametrize(
    ("argv", "module", "forwarded"),
    [
        (
            [
                "fetch",
                "demo",
                "--index",
                "2",
                "--workspace",
                "work dir",
                "--youtube-query",
                "a query",
            ],
            "fetch_vod",
            ["demo", "--index", "2", "--workspace", "work dir", "--youtube-query", "a query"],
        ),
        (
            ["match", "vod.wav", "hits.json", "a.wav", "b.wav"],
            "audio_match",
            ["vod.wav", "hits.json", "a.wav", "b.wav"],
        ),
        (["rank", "work dir"], "rank_moments", ["work dir", "15"]),
    ],
)
def test_analysis_commands_keep_stdout_json_and_restore_argv(
    argv, module, forwarded, monkeypatch, capsys
):
    import json
    import sys
    from types import SimpleNamespace

    original = sys.argv

    def stage():
        assert sys.argv == [module, *forwarded]
        print("Stage progress")

    def load(name):
        assert name == f"zunara.{module}"
        return SimpleNamespace(main=stage)

    monkeypatch.setattr(cli.importlib, "import_module", load)
    cli.main(argv)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"stage": argv[0], "status": "complete"}
    assert captured.err == "Stage progress\n"
    assert sys.argv is original


@pytest.mark.parametrize(
    ("command", "option"),
    [("fetch", "--youtube-query"), ("match", "query_audio"), ("rank", "top_n")],
)
def test_stage_help_describes_real_arguments_without_importing_dependencies(
    command, option, monkeypatch, capsys
):
    monkeypatch.setattr(cli.importlib, "import_module", lambda _: pytest.fail("Unexpected import"))
    with pytest.raises(SystemExit) as error:
        cli.main([command, "--help"])
    assert error.value.code == 0
    assert option in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv", [["fetch", "demo", "--index", "-1"], ["rank", "work", "0"], ["match"]]
)
def test_bad_stage_arguments_fail_before_loading_dependencies(argv, monkeypatch):
    monkeypatch.setattr(cli.importlib, "import_module", lambda _: pytest.fail("Unexpected import"))
    with pytest.raises(SystemExit) as error:
        cli.main(argv)
    assert error.value.code == 2


def test_failed_stage_restores_process_arguments(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    original = sys.argv

    def stage():
        raise ValueError("Synthetic stage failure")

    monkeypatch.setattr(cli.importlib, "import_module", lambda _: SimpleNamespace(main=stage))
    with pytest.raises(SystemExit):
        cli.main(["rank", "work"])
    assert sys.argv is original
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Synthetic stage failure" in captured.err
