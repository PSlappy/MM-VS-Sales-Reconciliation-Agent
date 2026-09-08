# MicroMart → VendSoft Daily Sync

Pulls a rolling 30-day itemized sales export from MicroMart, uploads it to a shared Google
Drive folder, imports it into VendSoft, and emails an HTML report either way. See the
[top-level README](../README.md) for the why and the architecture overview; this doc is the
operational reference for running and maintaining it.

## How it runs

- **Manually**: `.venv/bin/python src/sync.py` (add `--date 09-05-2026` to relabel the output
  filename -- it's not a filter, every run pulls the same rolling 30-day window;
  `--resume-from <step>` to continue after fixing a failure without redoing earlier steps). No
  email report — just runs the pipeline.
- **With reporting**: `.venv/bin/python src/run_daily.py` — same pipeline, labels output by
  today's date automatically, and always sends a success or failure email afterward. This is
  the exact entry point the schedule calls.
- **On a schedule**: a macOS `launchd` LaunchAgent
  (`~/Library/LaunchAgents/com.accessamenities.micromart-vendsoft-sync.plist`) runs
  `run_daily.py` headlessly every day at 8:00 AM. If the Mac is asleep at that moment, `launchd`
  runs it once as soon as the machine wakes rather than skipping the day. If a login attempt
  fails while headless, it automatically retries headed (see Failure handling below) — a visible
  Chrome window only appears when that happens.

Valid `--resume-from` step names: `micromart_login`, `micromart_filter_and_download`,
`upload_to_drive`, `vendsoft_login`, `vendsoft_import`.

## One-time setup

1. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium`
2. Store credentials in Keychain — see [`config/keychain-setup.md`](config/keychain-setup.md).
3. Set up Google OAuth (Drive + Gmail send) — see
   [`docs/google-drive-oauth-setup.md`](docs/google-drive-oauth-setup.md), then run
   `.venv/bin/python src/authorize_drive.py` once for the interactive consent.
4. Create `config/drive-folder-id.txt` (the target Drive folder's ID) and
   `config/notify-recipients.txt` (one email address per line) — see the adjacent
   `.example.txt` files. These are gitignored since they're environment-specific, not code.

## Failure handling

- Login attempts are capped at 2 tries — **not** retried aggressively, to avoid tripping
  account lockouts on MicroMart or VendSoft. If the first attempt was headless and failed, the
  retry escalates to a headed (visible) browser -- confirmed live that MicroMart blocks headless
  Chromium logins specifically, even with valid credentials and a valid TOTP code.
- On any failure or unexpected crash, the run halts, screenshots whichever browser page(s) were
  open at that moment (`debug-screenshots/`), and logs a resume command — it's designed to
  restart from the failed step, not redo the whole pipeline.
- `run_daily.py` turns all of that into an email: a screenshot of the failure state, the
  relevant slice of the run log (step timeline + every warning/error, not the full log), why it
  failed, and the exact command to resume after fixing it.
- MicroMart's MFA is handled automatically via a stored TOTP secret (`pyotp`) — no phone, no
  manual code entry. `src/totp_code.py` prints a fresh code on demand for manual use (e.g.
  finishing an MFA reset by hand). If a login ever hits an unrecognized screen (e.g. a real
  one-time-code challenge from a *different* auth method), it stops and asks for one manual
  login rather than guessing.
- **Known gap, by design**: a machine/product mapping that's genuinely *unresolved* (VendSoft
  shows a nonzero "Machines/Products to review" count) isn't automated -- the agent only
  auto-clicks the final import confirmation when everything already auto-resolved. An
  unresolved mapping stops safely and asks for a screenshot rather than guessing at a picker UI
  it's never seen.

## MicroMart CSV export behavior (confirmed)

No date filter is applied -- every run downloads "Last 30 Days" (the Download CSV modal's
default when no page-level date filter is set) via the Itemized Sales report type. A 30-day
export can take a minute or more to generate server-side, well past Playwright's normal 30s
default, so that download's timeout is set much higher.

## VendSoft quirks (confirmed)

- Deep-linking straight to the Sales Import URL renders blank for this account — the SPA's
  router expects client-side navigation state a fresh page load doesn't have. The script clicks
  through Configuration → Telemetry → Sales Import instead, same as a human would.
- The login form's "Email or username" / "Password" fields are Angular Material floating
  `<label>`s, not HTML `placeholder` attributes — target them with `get_by_label`, not
  `get_by_placeholder`.
- After attaching a file, VendSoft shows a mapping-review screen with "Machines to review" /
  "Products to review" counts. When both are 0 (everything auto-resolved), an "IMPORT N
  TRANSACTIONS" button appears and gets clicked automatically. A nonzero count means genuine
  manual mapping is needed -- see the known gap above.

## Files

- `src/sync.py` — the deterministic pipeline (login, download, upload, import)
- `src/run_daily.py` — scheduled entry point: runs the pipeline, sends the email report
- `src/email_report.py` — HTML email templates + Gmail API sending
- `src/authorize_drive.py` — one-time Google OAuth consent flow
- `src/totp_code.py` — prints a fresh 6-digit code for any Keychain-stored TOTP secret, for
  manual use (e.g. completing an MFA reset)
