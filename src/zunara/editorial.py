"""Optional per-platform topic policy. Without a policy file every platform is unrestricted."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLATFORMS = frozenset({"x", "youtube", "tiktok", "instagram"})
POLICY_FILE = "publishing-policy.json"
TOPIC = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _topic(value: object, where: str) -> str:
    if not isinstance(value, str) or not TOPIC.fullmatch(value):
        raise ValueError(
            f"{where} must be a lowercase topic name: 1–64 letters, digits, underscores or hyphens."
        )
    return value


@dataclass(frozen=True)
class TopicPolicy:
    allowed: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def allowed_topics(self, platform: str) -> frozenset[str] | None:
        """Return the allowed topics, or ``None`` when the platform is unrestricted."""
        if platform not in PLATFORMS:
            raise ValueError(f"Unknown platform: {platform}")
        return self.allowed.get(platform)

    def check(self, platform: str, rows: Iterable[Mapping[str, Any]]) -> None:
        """Raise one ValueError naming every row the platform's policy does not allow."""
        allowed = self.allowed_topics(platform)
        if allowed is None:
            return
        denied = []
        for row in rows:
            topic = row.get("topic")
            if topic is None:
                denied.append(f"{row['file']} (no topic)")
                continue
            _topic(topic, f"topic of {row['file']}")
            if topic not in allowed:
                denied.append(f"{row['file']} ({topic})")
        if denied:
            raise ValueError(
                f"Topic policy for {platform} allows only {', '.join(sorted(allowed))}; "
                f"blocked: {', '.join(denied)}. Nothing was exported or published."
            )


def parse_policy(data: object) -> TopicPolicy:
    if not isinstance(data, dict) or set(data) != {"platforms"}:
        raise ValueError('Publishing policy must be an object with only a "platforms" key.')
    platforms = data["platforms"]
    if not isinstance(platforms, dict):
        raise ValueError('"platforms" must be an object keyed by platform.')
    allowed: dict[str, frozenset[str]] = {}
    for platform, rules in platforms.items():
        if platform not in PLATFORMS:
            known = ", ".join(sorted(PLATFORMS))
            raise ValueError(f"Unknown platform in publishing policy: {platform} (use {known}).")
        if not isinstance(rules, dict) or set(rules) != {"allowed_topics"}:
            raise ValueError(f'Policy for {platform} must contain only "allowed_topics".')
        topics = rules["allowed_topics"]
        if not isinstance(topics, list) or not topics:
            raise ValueError(f"allowed_topics for {platform} must be a nonempty array.")
        allowed[platform] = frozenset(_topic(t, f"allowed_topics for {platform}") for t in topics)
    return TopicPolicy(allowed)


def load_policy(config_dir: Path) -> TopicPolicy:
    path = config_dir / POLICY_FILE
    if not path.exists() and not path.is_symlink():
        return TopicPolicy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read {POLICY_FILE}: expected a UTF-8 JSON file.") from error
    return parse_policy(data)
