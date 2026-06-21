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

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
    # Required to post comments as the channel owner (post_top_comment).
    # NOTE: tokens minted before this scope was added LACK it — upload keeps
    # working (it only needs youtube.upload), but comment posting will 403
    # until each channel is re-consented via `scripts/yt_init.py --channel <slug>`.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
# YouTube video category IDs — full list:
# https://developers.google.com/youtube/v3/docs/videoCategories/list
CATEGORY_EDUCATION = "27"
CATEGORY_SCIENCE_TECH = "28"
CATEGORY_ENTERTAINMENT = "24"
CATEGORY_PETS_ANIMALS = "15"
CATEGORY_HOWTO_STYLE = "26"
CATEGORY_PEOPLE_BLOGS = "22"


def credentials(channel: str) -> Credentials:
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
    default_language: str = "en",
    default_audio_language: str = "en",
    contains_synthetic_media: bool = True,
    license_: str = "youtube",
) -> dict:
    """Resumable upload. Returns {video_id, url, privacy}.

    Language fields:
      - default_language: language of the title/description (snippet.defaultLanguage)
      - default_audio_language: spoken language in the video (snippet.defaultAudioLanguage)
    contains_synthetic_media: YouTube's altered/synthetic content disclosure. True
      by default because the pipeline is fully AI-generated.
    license_: "youtube" (Standard YouTube License) or "creativeCommon".
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"privacy must be private|unlisted|public, got {privacy!r}")
    if license_ not in ("youtube", "creativeCommon"):
        raise ValueError(f"license must be youtube|creativeCommon, got {license_!r}")

    creds = credentials(channel)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": script.youtube.title,
            "description": script.youtube.description,
            "tags": script.youtube.tags,
            "categoryId": category_id,
            "defaultLanguage": default_language,
            "defaultAudioLanguage": default_audio_language,
        },
        "status": {
            "privacyStatus": privacy,
            "madeForKids": False,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": contains_synthetic_media,
            "license": license_,
            "embeddable": True,
            "publicStatsViewable": True,
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


def post_top_comment(channel: str, video_id: str, text: str) -> dict:
    """Post a top-level comment as the channel owner — the conversion "seed".

    Best-effort by design: requires the youtube.force-ssl scope, which tokens
    minted before that scope was added do NOT have. NEVER raises on an
    API/permission/network failure — it returns {posted: False, error: ...} so
    the upload pipeline always continues. Returns
    {posted: bool, comment_id: str | None, error: str | None}.

    (The Data API has no "pin" operation; a channel-owner top comment already
    surfaces prominently under a Short, which is the goal.)
    """
    if not text or not text.strip():
        return {"posted": False, "comment_id": None, "error": "empty comment text"}
    try:
        creds = credentials(channel)
        youtube = build("youtube", "v3", credentials=creds)
        resp = (
            youtube.commentThreads()
            .insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {
                            "snippet": {"textOriginal": text.strip()}
                        },
                    }
                },
            )
            .execute()
        )
        comment_id = resp["snippet"]["topLevelComment"]["id"]
        log.info(
            "seed comment posted: channel=%s video_id=%s comment_id=%s",
            channel, video_id, comment_id,
        )
        return {"posted": True, "comment_id": comment_id, "error": None}
    except Exception as e:  # noqa: BLE001 — best-effort; pipeline must not break
        log.warning(
            "seed comment failed (channel=%s video_id=%s): %s",
            channel, video_id, str(e)[:200],
        )
        return {"posted": False, "comment_id": None, "error": str(e)[:200]}
