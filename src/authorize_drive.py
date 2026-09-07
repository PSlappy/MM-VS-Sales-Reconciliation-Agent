#!/usr/bin/env python3
"""
One-time interactive Google OAuth consent for Drive + Gmail-send access.

Run this once (and again any time scopes change, or if the refresh token is
ever revoked):

    .venv/bin/python src/authorize_drive.py

Opens a browser window. Sign in as info@accessamenities.com and approve
access. The resulting refresh token is stored in Keychain -- the daily
sync script reads it from there and never needs the browser again.
"""
import subprocess
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRETS_FILE = ROOT / "config" / "oauth-client.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]
KEYCHAIN_SERVICE = "vendsoft-drive-oauth-refresh-token"


def store_in_keychain(token: str) -> None:
    import os
    subprocess.run(
        ["security", "add-generic-password", "-a", os.environ["USER"],
         "-s", KEYCHAIN_SERVICE, "-w", token, "-U"],
        check=True,
    )


def main() -> None:
    if not CLIENT_SECRETS_FILE.exists():
        print(f"Missing {CLIENT_SECRETS_FILE} -- see docs/google-drive-oauth-setup.md", file=sys.stderr)
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        print(
            "No refresh token returned. This can happen on a re-consent. "
            "Revoke prior access at https://myaccount.google.com/permissions "
            "for this app and re-run.", file=sys.stderr,
        )
        sys.exit(1)

    store_in_keychain(creds.refresh_token)
    print(f"Refresh token stored in Keychain as '{KEYCHAIN_SERVICE}'. Done.")


if __name__ == "__main__":
    main()
