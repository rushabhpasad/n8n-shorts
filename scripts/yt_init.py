#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-auth-oauthlib>=1.2",
#   "google-api-python-client>=2.150",
# ]
# ///
"""One-time YouTube OAuth bootstrap.

Run this once on a machine WITH a browser (your laptop/desktop, not stl over
SSH). It launches a local server, opens a browser to Google's consent screen,
and writes out a token file that contains a refresh token.

The refresh token lives effectively forever (Google rotates only on user
revoke or 6 months of inactivity), so once written, the FastAPI service can
mint fresh access tokens on demand without further interaction.

INPUT:
  ~/temp/etymology-shorts/secrets/youtube_oauth.json
    OAuth 2.0 Desktop Client ID JSON downloaded from Google Cloud Console.

OUTPUT:
  ~/temp/etymology-shorts/secrets/youtube_token.json
    Long-lived credentials. SYNC THIS TO stl AFTER WRITING:
      rsync -avh secrets/youtube_token.json stl:etymology-shorts/secrets/

USAGE:
  cd /Users/rpasad/temp/etymology-shorts
  ./scripts/yt_init.py
  # or
  uv run --no-project scripts/yt_init.py

The script is uv-script-shebanged — it pulls its own deps into an ephemeral
venv. No project setup needed locally.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = PROJECT_ROOT / "secrets"
CLIENT_SECRETS = SECRETS_DIR / "youtube_oauth.json"
TOKEN_PATH = SECRETS_DIR / "youtube_token.json"


def main() -> int:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    if not CLIENT_SECRETS.exists():
        print(
            f"ERROR: {CLIENT_SECRETS} not found.\n"
            f"  1. Visit https://console.cloud.google.com/apis/credentials\n"
            f"  2. Create OAuth 2.0 Client ID (type: Desktop app) under your project\n"
            f"  3. Download JSON and save it to:\n"
            f"     {CLIENT_SECRETS}\n",
            file=sys.stderr,
        )
        return 1

    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        print(f"Existing token at {TOKEN_PATH} — refreshing if possible…")
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds.valid:
            print("Token already valid. Nothing to do.")
        elif creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            print("Token refreshed and rewritten.")
        else:
            print("Existing token is not refreshable — running full flow.")
            creds = None

    if creds is None or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRETS), SCOPES
        )
        # Opens a browser to Google's consent screen. The local server here
        # catches the redirect with the auth code.
        creds = flow.run_local_server(
            host="localhost",
            port=0,                    # any free port
            authorization_prompt_message=(
                "\nA browser tab is opening for Google sign-in. Sign in with "
                "the account that owns the target YouTube channel.\n"
            ),
            success_message=(
                "Authorization complete. You can close this browser tab and "
                "return to the terminal."
            ),
            open_browser=True,
        )
        TOKEN_PATH.write_text(creds.to_json())
        print(f"Token written to {TOKEN_PATH}")

    # Smoke-test: list the authorised channel(s) so the user can confirm the
    # right account is wired up. Best-effort — channels.list requires
    # youtube.readonly, which we don't request (minimum-privilege for the
    # upload scope only). If it fails we print a hint instead of crashing.
    try:
        from googleapiclient.errors import HttpError

        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            print(
                "WARNING: this Google account has no YouTube channel attached.\n"
                "  Create one at https://youtube.com (click profile > Create channel),\n"
                "  then re-run this script."
            )
            return 2
        ch = items[0]["snippet"]
        print()
        print("──────────────────────────────────────────────")
        print(f"  Channel name : {ch['title']}")
        print(f"  Channel ID   : {items[0]['id']}")
        print(f"  Description  : {(ch.get('description') or '')[:80]}")
        print("──────────────────────────────────────────────")
    except HttpError as e:
        if e.resp.status == 403:
            print()
            print("Channel-verification skipped (token has upload scope only —")
            print("this is by design). Open YouTube Studio in a browser signed")
            print("into the same Google account to confirm the target channel.")
        else:
            print(f"Channel verification failed: {e}")
    print()
    print("Next steps:")
    print(f"  1. rsync -avh {TOKEN_PATH} stl:etymology-shorts/secrets/")
    print(f"  2. rsync -avh {CLIENT_SECRETS} stl:etymology-shorts/secrets/")
    print("     (shorts-api on stl needs both to refresh the token later)")
    print("  3. Test upload:")
    print(
        '     ssh stl \'curl -sS -X POST -H "content-type: application/json" '
        '-d "{\\"word_id\\":1,\\"privacy\\":\\"private\\"}" '
        'http://localhost:7860/upload\''
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
