"""Local signal edge cases and subtitle injection boundaries, without model loading."""

import json

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
