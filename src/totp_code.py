#!/usr/bin/env python3
"""
Print the current 6-digit TOTP code for a Keychain-stored secret.

Usage:
    .venv/bin/python src/totp_code.py                            # micromart-platform-totp-secret
    .venv/bin/python src/totp_code.py vendsoft-next-totp-secret   # any other stored TOTP secret

The code is valid for ~30 seconds -- use it right away.
"""
import os
import subprocess
import sys

import pyotp

DEFAULT_SERVICE = "micromart-platform-totp-secret"


def main() -> None:
    service = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERVICE
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ["USER"], "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Could not read '{service}' from Keychain.", file=sys.stderr)
        sys.exit(1)
    secret = result.stdout.strip()
    print(pyotp.TOTP(secret).now())


if __name__ == "__main__":
    main()
