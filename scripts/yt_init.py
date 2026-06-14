#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-auth-oauthlib>=1.2",
#   "google-api-python-client>=2.150",
# ]
# ///
"""One-time YouTube OAuth bootstrap, per channel.

Run this once per channel on a machine WITH a browser. It launches a local
server, opens a browser tab to Google's consent screen, and writes out a
channel-specific token file containing the refresh token.

INPUT:
  secrets/youtube_oauth.<channel>.json
    OAuth 2.0 Desktop Client ID JSON from Google Cloud Console.

OUTPUT:
  secrets/youtube_token.<channel>.json
    Long-lived credentials. The FastAPI service reads this on every /upload
    and auto-refreshes the access token as needed.

USAGE:
  uv run scripts/yt_init.py --channel wordstrata
  uv run scripts/yt_init.py --channel the-mythscape
  uv run scripts/yt_init.py --channel open-verdicts
  uv run scripts/yt_init.py --channel bright-beasts

You need ONE OAuth Client per Google account / YouTube channel. Create it in
the Cloud project you've enabled YouTube Data API v3 on, then download the
client-secret JSON and save it to secrets/youtube_oauth.<channel>.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = PROJECT_ROOT / "secrets"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True,
                    help="channel slug — must match channels/<slug>/channel.json")
    args = ap.parse_args()

    channel = args.channel
    client_secrets = SECRETS_DIR / f"youtube_oauth.{channel}.json"
    token_path = SECRETS_DIR / f"youtube_token.{channel}.json"

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    if not client_secrets.exists():
        print(
            f"ERROR: {client_secrets} not found.\n"
            f"  1. Visit https://console.cloud.google.com/apis/credentials\n"
            f"  2. Create an OAuth 2.0 Client ID (type: Desktop app) for the\n"
            f"     Google account that owns the {channel} YouTube channel.\n"
            f"  3. Download the JSON and save it as:\n"
            f"     {client_secrets}\n",
            file=sys.stderr,
        )
        return 1

    creds: Credentials | None = None
    if token_path.exists():
        print(f"Existing token at {token_path} — refreshing if possible…")
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.valid:
            print("Token already valid. Nothing to do.")
        elif creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            print("Token refreshed and rewritten.")
        else:
            print("Existing token is not refreshable — running full flow.")
            creds = None

    if creds is None or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets), SCOPES
        )
        creds = flow.run_local_server(
            host="localhost",
            port=0,
            authorization_prompt_message=(
                f"\nA browser tab is opening for Google sign-in. Sign in with "
                f"the account that owns the {channel} YouTube channel.\n"
            ),
            success_message=(
                "Authorization complete. You can close this browser tab and "
                "return to the terminal."
            ),
            open_browser=True,
        )
        token_path.write_text(creds.to_json())
        print(f"Token written to {token_path}")

    # Best-effort channel-verification smoke test (requires youtube.readonly,
    # which we don't request — so it'll typically 403). Print a hint either way.
    try:
        from googleapiclient.errors import HttpError

        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            print(
                f"WARNING: the Google account authorized for {channel} has no "
                f"YouTube channel attached.\n"
                f"  Create one at https://youtube.com (profile > Create channel),\n"
                f"  then re-run this script."
            )
            return 2
        ch = items[0]["snippet"]
        print()
        print("──────────────────────────────────────────────")
        print(f"  Channel slug : {channel}")
        print(f"  Channel name : {ch['title']}")
        print(f"  Channel ID   : {items[0]['id']}")
        print(f"  Description  : {(ch.get('description') or '')[:80]}")
        print("──────────────────────────────────────────────")
    except HttpError as e:
        if e.resp.status == 403:
            print()
            print("Channel-verification skipped (token has upload scope only —")
            print("this is by design). Open YouTube Studio signed in as the")
            print(f"{channel} owner to confirm the target channel.")
        else:
            print(f"Channel verification failed: {e}")
    print()
    print("Next steps:")
    print(f"  1. {token_path} now exists.")
    print("  2. Test an upload (word_id=1 must have been through /script,")
    print("     /voice, /image, /assemble first):")
    print(
        '     curl -sS -X POST -H "content-type: application/json" '
        '-d \'{"word_id":1}\' '
        f"http://localhost:7860/{channel}/upload"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
