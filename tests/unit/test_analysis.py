"""Local signal edge cases and subtitle injection boundaries, without model loading."""

import json
import tomllib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from zunara import signals
from zunara.render import escape_ass, render


def test_short_smoothing_keeps_signal_length_and_empty_is_empty():
    for window in (0, 1, 600):
        assert len(signals.smooth(np.array([1.0, 2.0]), window)) == 2
        assert len(signals.zscore(np.array([1.0, 2.0]), window)) == 2
        assert len(signals.smooth(np.array([]), window)) == 0
        assert len(signals.zscore(np.array([]), window)) == 0


def test_empty_chat_has_zero_rate_and_unsorted_samples_interpolate(tmp_path):
    path = tmp_path / "chat.json"
    path.write_text("[]")
    np.testing.assert_array_equal(signals.chat_per_second(path, 3), [0, 0, 0])
    path.write_text(json.dumps([{"offset": 2, "per_min": 60}, {"offset": 0, "per_min": 0}]))
    np.testing.assert_array_equal(signals.chat_per_second(path, 3), [0, 30, 60])


def test_loudness_handles_empty_and_stereo_tracks(tmp_path):
    path = tmp_path / "audio.wav"
    sf.write(path, np.empty((0, 2)), 16000)
    assert signals.loudness_per_second(path).size == 0
    sf.write(path, np.full((16000, 2), 0.5), 16000)
    np.testing.assert_allclose(signals.loudness_per_second(path), [-6.0206], atol=0.01)


def test_subtitle_text_cannot_inject_ass_override_codes():
    escaped = escape_ass("{\\pos(1,2)}text\nnext")
    assert "{" not in escaped and "}" not in escaped
    assert "\\pos" not in escaped
    assert "\n" not in escaped


@pytest.mark.parametrize(
    ("start", "end"), [(-1, 2), (2, 2), (3, 2), (float("nan"), 5), (0, float("inf"))]
)
def test_invalid_render_ranges_fail_before_model_or_file_access(tmp_path, start, end):
    with pytest.raises(ValueError, match="interval"):
        render(tmp_path, start, end, "Hook", "clip")
    assert not list(tmp_path.iterdir())


def test_render_rejects_path_traversal_before_model_or_file_access(tmp_path):
    with pytest.raises(ValueError):
        render(tmp_path, 0, 5, "Hook", "../escape")
    assert not list(tmp_path.iterdir())


def test_asr_extra_installs_the_model_downloader():
    # onnx-asr only pulls huggingface-hub through its own "hub" extra; asr_model needs it.
    project = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    names = [
        r.split(">")[0].split("[")[0] for r in project["project"]["optional-dependencies"]["asr"]
    ]
    assert "huggingface-hub" in names


def test_missing_asr_extra_has_an_install_instruction(monkeypatch):
    import sys

    from zunara import render as renderer

    monkeypatch.setattr(renderer, "_ASR", None)
    monkeypatch.setitem(sys.modules, "onnx_asr", None)
    with pytest.raises(ValueError, match="asr extra"):
        renderer.asr_model()


def test_uncached_offline_model_explains_recovery_without_private_payload(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from zunara import render as renderer

    class MissingWeights(Exception):
        pass

    def fail(*args, **kwargs):
        raise MissingWeights("SYNTHETIC_PRIVATE_DOWNLOAD_PAYLOAD")

    errors = ModuleType("huggingface_hub.errors")
    errors.LocalEntryNotFoundError = MissingWeights
    monkeypatch.setattr(renderer, "_ASR", None)
    monkeypatch.setitem(sys.modules, "onnx_asr", SimpleNamespace(load_model=fail))
    monkeypatch.setitem(sys.modules, "huggingface_hub", ModuleType("huggingface_hub"))
    monkeypatch.setitem(sys.modules, "huggingface_hub.errors", errors)
    with pytest.raises(ValueError) as error:
        renderer.asr_model()
    assert "HF_HUB_OFFLINE" in str(error.value)
    assert "first download" in str(error.value)
    assert "SYNTHETIC_PRIVATE_DOWNLOAD_PAYLOAD" not in str(error.value)
