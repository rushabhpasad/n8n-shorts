"""YouTube Shorts upload via YouTube Data API v3.

Per-channel OAuth — each channel has its own OAuth client + refresh token
written by `scripts/yt_init.py --channel <slug>`. Stored at:
  secrets/youtube_oauth.<channel>.json
  secrets/youtube_token.<channel>.json
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import settings
from models import Script

log = logging.getLogger("shorts-api.youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_EDUCATION = "27"
CATEGORY_SCIENCE_TECH = "28"


def _credentials(channel: str) -> Credentials:
    token_path = settings.youtube_token_path(channel)
    if not token_path.exists():
        raise RuntimeError(
            f"YouTube token not found at {token_path}. "
            f"Run `uv run scripts/yt_init.py --channel {channel}` once to "
            f"authorize this channel."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("refreshing YouTube OAuth token for channel=%s", channel)
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        else:
            raise RuntimeError(
                f"YouTube creds invalid and not refreshable for channel={channel}"
                f" — re-run yt_init.py --channel {channel}"
            )
    return creds


def upload_short(
    channel: str,
    script: Script,
    video_path: Path,
    privacy: str = "public",
    category_id: str = CATEGORY_EDUCATION,
) -> dict:
    """Resumable upload. Returns {video_id, url, privacy}."""
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"privacy must be private|unlisted|public, got {privacy!r}")

    creds = _credentials(channel)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": script.youtube.title,
            "description": script.youtube.description,
            "tags": script.youtube.tags,
            "categoryId": category_id,
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy,
            "madeForKids": False,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        chunksize=4 * 1024 * 1024,
        resumable=True,
        mimetype="video/mp4",
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status is not None:
            log.info("YT upload progress (channel=%s): %d%%", channel, int(status.progress() * 100))

    video_id = response["id"]
    log.info("YT upload done: channel=%s video_id=%s privacy=%s", channel, video_id, privacy)
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/shorts/{video_id}",
        "privacy": privacy,
    }
