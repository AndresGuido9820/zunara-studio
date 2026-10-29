import json
from pathlib import Path
from typing import Any

import pytest

from zunara.editorial import POLICY_FILE, TopicPolicy, load_policy, parse_policy

X_PROGRAMMING = {"platforms": {"x": {"allowed_topics": ["programming"]}}}


def write_policy(config: Path, content: object) -> Path:
    config.mkdir(parents=True, exist_ok=True)
    path = config / POLICY_FILE
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    return path


def rows(*topics: Any) -> list[dict[str, Any]]:
    result = []
    for number, topic in enumerate(topics):
        row: dict[str, Any] = {"file": f"clip{number}.mp4"}
        if topic is not ...:
            row["topic"] = topic
        result.append(row)
    return result


# Loading


def test_missing_file_is_unrestricted(tmp_path: Path) -> None:
    policy = load_policy(tmp_path / "config")

    for platform in ("x", "youtube", "tiktok", "instagram"):
        assert policy.allowed_topics(platform) is None
        policy.check(platform, rows(..., "anything", None))
    assert not (tmp_path / "config").exists()


def test_valid_file_restricts_only_listed_platforms(tmp_path: Path) -> None:
    write_policy(tmp_path, X_PROGRAMMING)

    policy = load_policy(tmp_path)

    assert policy.allowed_topics("x") == {"programming"}
    assert policy.allowed_topics("youtube") is None


def test_multiple_topics_and_platforms() -> None:
    policy = parse_policy(
        {
            "platforms": {
                "x": {"allowed_topics": ["programming", "open-source"]},
                "youtube": {"allowed_topics": ["gaming"]},
            }
        }
    )

    assert policy.allowed_topics("x") == {"programming", "open-source"}
    assert policy.allowed_topics("youtube") == {"gaming"}


@pytest.mark.parametrize("content", ["{", "", "﻿{}x", '{"platforms": {}} trailing'])
def test_unreadable_json_is_rejected(tmp_path: Path, content: str) -> None:
    write_policy(tmp_path, content)

    with pytest.raises(ValueError, match=POLICY_FILE):
        load_policy(tmp_path)


def test_non_utf8_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / POLICY_FILE).write_bytes(b"\xff\xfe\x00{")

    with pytest.raises(ValueError, match=POLICY_FILE):
        load_policy(tmp_path)


def test_directory_in_place_of_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / POLICY_FILE).mkdir()

    with pytest.raises(ValueError, match=POLICY_FILE):
        load_policy(tmp_path)


def test_dangling_symlink_fails_closed(tmp_path: Path) -> None:
    (tmp_path / POLICY_FILE).symlink_to(tmp_path / "missing.json")

    with pytest.raises(ValueError, match=POLICY_FILE):
        load_policy(tmp_path)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], 'only a "platforms" key'),
        ({}, 'only a "platforms" key'),
        ({"platforms": {}, "version": 1}, 'only a "platforms" key'),
        ({"platform": {}}, 'only a "platforms" key'),
        ({"platforms": []}, "keyed by platform"),
        ({"platforms": {"twitter": {"allowed_topics": ["a"]}}}, "Unknown platform.*twitter"),
        ({"platforms": {"X": {"allowed_topics": ["a"]}}}, "Unknown platform"),
        ({"platforms": {"x": ["programming"]}}, 'only "allowed_topics"'),
        ({"platforms": {"x": {}}}, 'only "allowed_topics"'),
        ({"platforms": {"x": {"allowed_topic": ["a"]}}}, 'only "allowed_topics"'),
        ({"platforms": {"x": {"allowed_topics": ["a"], "denied": []}}}, 'only "allowed_topics"'),
        ({"platforms": {"x": {"allowed_topics": "programming"}}}, "nonempty array"),
        ({"platforms": {"x": {"allowed_topics": []}}}, "nonempty array"),
        ({"platforms": {"x": {"allowed_topics": [1]}}}, "lowercase topic"),
        ({"platforms": {"x": {"allowed_topics": [None]}}}, "lowercase topic"),
        ({"platforms": {"x": {"allowed_topics": [""]}}}, "lowercase topic"),
        ({"platforms": {"x": {"allowed_topics": ["Programming"]}}}, "lowercase topic"),
        ({"platforms": {"x": {"allowed_topics": [" programming"]}}}, "lowercase topic"),
        ({"platforms": {"x": {"allowed_topics": ["a" * 65]}}}, "lowercase topic"),
    ],
)
def test_malformed_policy_is_rejected(tmp_path: Path, data: object, message: str) -> None:
    write_policy(tmp_path, data)

    with pytest.raises(ValueError, match=message):
        load_policy(tmp_path)


def test_empty_platforms_object_is_unrestricted() -> None:
    assert parse_policy({"platforms": {}}).allowed_topics("x") is None


def test_unknown_platform_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown platform"):
        TopicPolicy().check("facebook", [])


# Checking rows


def test_allowed_topic_passes() -> None:
    parse_policy(X_PROGRAMMING).check("x", rows("programming", "programming"))


def test_empty_batch_passes() -> None:
    parse_policy(X_PROGRAMMING).check("x", [])


@pytest.mark.parametrize("topic", [..., None, "gaming", "programming-news"])
def test_missing_or_disallowed_topic_is_denied(topic: object) -> None:
    with pytest.raises(ValueError, match="allows only programming"):
        parse_policy(X_PROGRAMMING).check("x", rows("programming", topic))


@pytest.mark.parametrize(
    "topic", [["programming"], 1, True, {"name": "programming"}, "Programming", ""]
)
def test_malformed_row_topic_is_rejected(topic: object) -> None:
    with pytest.raises(ValueError, match="topic of clip0.mp4"):
        parse_policy(X_PROGRAMMING).check("x", rows(topic))


def test_error_lists_every_blocked_clip() -> None:
    with pytest.raises(ValueError) as error:
        parse_policy(X_PROGRAMMING).check("x", rows("gaming", "programming", ...))

    message = str(error.value)
    assert "clip0.mp4 (gaming)" in message
    assert "clip2.mp4 (no topic)" in message
    assert "clip1.mp4" not in message


def test_other_platforms_ignore_x_policy() -> None:
    policy = parse_policy(X_PROGRAMMING)

    for platform in ("youtube", "tiktok", "instagram"):
        policy.check(platform, rows(..., "gaming"))
