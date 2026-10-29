"""Portable paths. Importing this module never creates files."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path, user_data_path


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Use 1–80 letters, digits, underscores or hyphens for names.")
    return value


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    data_dir: Path
    client_secret: Path

    @classmethod
    def from_env(cls) -> "Settings":
        config = Path(os.environ.get("ZUNARA_CONFIG_DIR") or user_config_path("zunara"))
        data = Path(os.environ.get("ZUNARA_DATA_DIR") or user_data_path("zunara"))
        secret = Path(
            os.environ.get("ZUNARA_YOUTUBE_CLIENT_SECRET") or config / "client_secret.json"
        )
        return cls(
            config.expanduser().resolve(),
            data.expanduser().resolve(),
            secret.expanduser().resolve(),
        )

    def token_path(self, channel: str) -> Path:
        return self.config_dir / "tokens" / f"{safe_name(channel)}.json"
