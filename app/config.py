"""Runtime configuration, read from environment variables.

On Cloud Run every secret is mounted from Secret Manager as an env var.
Nothing in here is ever logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


def _json_or_path(value: str) -> dict | None:
    """Accept either raw JSON or a path to a JSON file (handy for local dev)."""
    value = value.strip()
    if not value:
        return None
    if value.startswith("{"):
        return json.loads(value)
    return json.loads(Path(value).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    google_sa_info: dict | None = None
    # OAuth user token for Drive. Needed on personal Google accounts, where a
    # service account has no storage quota and cannot own uploaded files.
    google_drive_token_info: dict | None = None
    sheet_id: str = ""
    drive_folder_id: str = ""
    allowed_user_ids: frozenset[str] = field(default_factory=frozenset)
    owner_user_id: str = ""
    tz_name: str = "Asia/Bangkok"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        allowed = frozenset(
            u.strip() for u in env.get("ALLOWED_USER_IDS", "").split(",") if u.strip()
        )
        owner = env.get("OWNER_USER_ID", "").strip() or (sorted(allowed)[0] if allowed else "")
        return cls(
            line_channel_secret=env.get("LINE_CHANNEL_SECRET", ""),
            line_channel_access_token=env.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
            google_sa_info=_json_or_path(env.get("GOOGLE_SA_JSON", "")),
            google_drive_token_info=_json_or_path(env.get("GOOGLE_DRIVE_TOKEN_JSON", "")),
            sheet_id=env.get("SHEET_ID", ""),
            drive_folder_id=env.get("DRIVE_FOLDER_ID", ""),
            allowed_user_ids=allowed,
            owner_user_id=owner,
            tz_name=env.get("TZ", "Asia/Bangkok") or "Asia/Bangkok",
        )


@lru_cache
def get_settings() -> Settings:
    # Local dev: pick up a .env file in the working directory. Real env vars
    # (e.g. Cloud Run secrets) win; the file is gitignored and not in the image.
    from dotenv import load_dotenv

    load_dotenv(override=False)
    return Settings.from_env()
