"""YouTube Shorts upload via YouTube Data API v3.

Reads OAuth credentials from settings.youtube_token_path. The token is created
by the one-time bootstrap script `scripts/yt_init.py` (run on a machine with a
browser), then auto-refreshed on every use here.

Shorts detection at YouTube is purely heuristic — there is no "Shorts" API.
A video is treated as a Short if it's vertical, ≤ 60s, and the title/description
contains '#shorts'. Our script LLM always emits #shorts in the title; the
ffmpeg assembler outputs 1080×1920; durations come from the WAV which Piper
keeps under 60s for our 80–110 word narration.
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


def _credentials() -> Credentials:
    if not settings.youtube_token_path.exists():
        raise RuntimeError(
            f"YouTube token not found at {settings.youtube_token_path}. "
            f"Run `python scripts/yt_init.py` once to authorize, then sync the "
            f"resulting token.json to {settings.youtube_token_path}."
        )
    creds = Credentials.from_authorized_user_file(
        str(settings.youtube_token_path), SCOPES
    )
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            log.info("refreshing YouTube OAuth token")
            creds.refresh(Request())
            settings.youtube_token_path.write_text(creds.to_json())
        else:
            raise RuntimeError(
                "YouTube creds invalid and not refreshable — re-run yt_init.py"
            )
    return creds


def upload_short(
    script: Script,
    video_path: Path,
    privacy: str = "private",
) -> dict:
    """Resumable upload. Returns {video_id, url, privacy}."""
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"privacy must be private|unlisted|public, got {privacy!r}")

    creds = _credentials()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": script.youtube.title,
            "description": script.youtube.description,
            "tags": script.youtube.tags,
            "categoryId": CATEGORY_EDUCATION,
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
            log.info("YT upload progress: %d%%", int(status.progress() * 100))

    video_id = response["id"]
    log.info("YT upload done: video_id=%s privacy=%s", video_id, privacy)
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/shorts/{video_id}",
        "privacy": privacy,
    }
