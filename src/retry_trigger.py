#!/usr/bin/env python3
"""
Shared helpers for the "Retry" button in failure emails.

This Mac can't receive an inbound click from the internet without opening it up to the
network in a way that's a much bigger step than we've taken so far (no port forwarding, no
dynamic DNS). So instead, a tiny Google Apps Script Web App (deployed once by hand -- see
docs/retry-trigger-setup.md) acts as the public receiver for the email click, and this Mac
only ever makes outbound polling requests to it on its own schedule (see
check_retry_trigger.py) -- same trust model as everything else this tool already does
(Drive/Gmail API calls out, nothing listens for inbound connections). That's also why a
retry isn't instant: expect it within a few minutes of the click, and only while the Mac is
on and awake.
"""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBAPP_URL_FILE = ROOT / "config" / "retry-webapp-url.txt"
KEYCHAIN_SERVICE = "mm-vs-retry-webapp-secret"


def _keychain_secret(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ["USER"], "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not read '{service}' from Keychain. See docs/retry-trigger-setup.md."
        )
    return result.stdout.strip()


def get_webapp_url() -> str:
    if not WEBAPP_URL_FILE.exists():
        raise RuntimeError(
            f"No retry Web App URL configured. Add it to {WEBAPP_URL_FILE} -- "
            "see docs/retry-trigger-setup.md."
        )
    url = WEBAPP_URL_FILE.read_text().strip()
    if not url:
        raise RuntimeError(f"{WEBAPP_URL_FILE} is empty -- see docs/retry-trigger-setup.md.")
    return url


def build_retry_link(date_str: str) -> str:
    """Link for the failure email's Retry button. Landing on it shows a confirmation page
    rather than instantly setting the flag -- email link-scanners/prefetchers sometimes fetch
    links automatically just from the email being opened, and this keeps that harmless."""
    secret = _keychain_secret(KEYCHAIN_SERVICE)
    params = urllib.parse.urlencode({"action": "retry", "token": secret, "date": date_str})
    return f"{get_webapp_url()}?{params}"


def check_and_clear(date_str: str, timeout: int = 15) -> bool:
    """Polled by check_retry_trigger.py. Returns True if a retry was requested for this exact
    date, and clears the flag on the Web App side so it's only acted on once."""
    secret = _keychain_secret(KEYCHAIN_SERVICE)
    params = urllib.parse.urlencode({"action": "check", "token": secret, "date": date_str})
    url = f"{get_webapp_url()}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach the retry Web App: {e}")
    return bool(data.get("retry_requested"))
