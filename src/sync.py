#!/usr/bin/env python3
"""
Daily MicroMart -> Google Drive -> VendSoft sales sync.

Pulls a rolling "Last 30 Days" itemized export every run (not a single day) -- MicroMart
excludes failed-payment rows from a day's export entirely, and a retried payment that later
succeeds keeps its *original* transaction date. A 30-day window plus VendSoft's own
per-transaction duplicate detection means a newly-resolved retry gets picked up within a day
of becoming successful, instead of being permanently missed by a single-day pull.

Manual run:       python src/sync.py
Relabel output:   python src/sync.py --date 09-05-2026  (label date for filenames, not a filter)
Resume a step:    python src/sync.py --resume-from vendsoft_import

Steps are deterministic (Playwright), not agent-driven -- a wrapping scheduled
Claude Code task is responsible for triggering this daily, reading the result,
and emailing a report. This script's job is just to do the work and report
its own status clearly via logs + exit code.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
BROWSER_STATE_DIR = ROOT / "browser-state"
DEBUG_DIR = ROOT / "debug-screenshots"

TARGET_DIR = (
    Path.home() / "Desktop" / "Access-Amenities" / "VendSoft" / "Sales-Import"
    / "MicroMart-Transactions-Itemized-Sales"
)

MICROMART_BASE = "https://platform.micromart.com"
MICROMART_DASHBOARD = f"{MICROMART_BASE}/dashboard"
MICROMART_TRANSACTIONS = f"{MICROMART_BASE}/dashboard/transactions"
MICROMART_LOGIN_FRAGMENT = "auth.micromart.com"

VENDSOFT_BASE = "https://secure.vendsoft.com/next"
VENDSOFT_SALES_IMPORT = f"{VENDSOFT_BASE}/configuration/telemetry/import"
VENDSOFT_LOGIN_FRAGMENT = "/next/login"

OAUTH_CLIENT_FILE = ROOT / "config" / "oauth-client.json"
DRIVE_FOLDER_ID_FILE = ROOT / "config" / "drive-folder-id.txt"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
KEYCHAIN_DRIVE_REFRESH = "vendsoft-drive-oauth-refresh-token"

MAX_LOGIN_ATTEMPTS = 2  # capped low deliberately -- avoid tripping account lockouts

# Best-effort guesses -- neither site's OTP screen has been seen yet. Update once
# we hit a real one so login detection doesn't misfire.
OTP_PROBE_TEXTS = [
    "verification code",
    "one-time code",
    "enter the code",
    "two-factor",
    "2FA",
]

HEADLESS = os.environ.get("SYNC_HEADLESS") == "1"


class StepFailed(RuntimeError):
    """Raised by a step to halt the run. The step name is used for --resume-from."""


def _goto(page, url: str, retries: int = 3, delay_seconds: float = 5) -> None:
    """page.goto with a few retries for transient network blips (e.g. right after the Mac
    wakes from sleep and Wi-Fi is still reconnecting -- exactly when a launchd catch-up run
    is likely to fire)."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url)
            return
        except PlaywrightError as e:
            last_error = e
            if attempt < retries:
                time.sleep(delay_seconds)
    raise last_error


def _extract_vendsoft_stats(page_text: str, outcome: str) -> dict:
    """Best-effort structured numbers out of VendSoft's import/review screen text, for a
    clean email table instead of a raw text dump."""
    def _num(pattern):
        m = re.search(pattern, page_text, re.I)
        return int(m.group(1)) if m else None

    header = re.search(r"(\d+)\s*rows?\s*[·\-]\s*(\d+)\s*txns?\s*[·\-]\s*(\d+)\s*products?",
                        page_text, re.I)
    return {
        "outcome": outcome,
        "rows": int(header.group(1)) if header else None,
        "txns": int(header.group(2)) if header else None,
        "products": int(header.group(3)) if header else None,
        "machines_matched": _num(r"(\d+)\s*machines?\s*matched"),
        "duplicates": _num(r"(\d+)\s*exact duplicates"),
        # "Ready transactions" appears pre-confirmation, "Transactions materialized" after.
        "imported": _num(r"(?:Ready transactions|Transactions materialized)\D*(\d+)"),
    }


def _wait_settled(page, timeout: int = 10000) -> None:
    """Best-effort settle wait. Some pages (e.g. MicroMart's dashboard, with a live-updating
    chart and chat widget) never truly reach networkidle -- don't treat that as fatal. By the
    time this times out, the page is almost always already fully interactive."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def get_keychain_secret(service: str, setup_hint: str = "config/keychain-setup.md") -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ["USER"], "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise StepFailed(f"Could not read '{service}' from Keychain. See {setup_hint}.")
    return result.stdout.strip()


def _drive_service():
    import json
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    client_config = json.loads(OAUTH_CLIENT_FILE.read_text())["installed"]
    refresh_token = get_keychain_secret(
        KEYCHAIN_DRIVE_REFRESH, setup_hint="src/authorize_drive.py (run it once)"
    )
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=creds)


class Context:
    def __init__(self, target_date: date, log: logging.Logger):
        self.target_date = target_date
        self.log = log
        self._playwright = None
        self._micromart_ctx = None
        self._vendsoft_ctx = None

    @property
    def date_dashed(self) -> str:
        return self.target_date.strftime("%m-%d-%Y")

    @property
    def csv_filename(self) -> str:
        return f"transaction-items-last30days-{self.date_dashed}.csv"

    def _pw(self):
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        return self._playwright

    def _persistent_page(self, attr: str, profile_name: str, force_headed: bool = False):
        existing = getattr(self, attr)
        if force_headed and existing is not None:
            # Escalating an already-open (headless) context to headed -- close it and
            # relaunch against the same profile dir, so the session/cookies carry over.
            try:
                existing.close()
            except Exception:
                pass
            existing = None
            setattr(self, attr, None)
        if existing is None:
            profile_dir = BROWSER_STATE_DIR / profile_name
            profile_dir.mkdir(parents=True, exist_ok=True)
            headless = False if force_headed else HEADLESS
            existing = self._pw().chromium.launch_persistent_context(
                str(profile_dir), headless=headless
            )
            setattr(self, attr, existing)
        return existing.pages[0] if existing.pages else existing.new_page()

    def micromart_page(self, force_headed: bool = False):
        return self._persistent_page("_micromart_ctx", "micromart-profile", force_headed)

    def vendsoft_page(self, force_headed: bool = False):
        return self._persistent_page("_vendsoft_ctx", "vendsoft-profile", force_headed)

    def screenshot_all(self, tag: str) -> list:
        """Screenshot whichever browser page(s) are currently open, for failure diagnostics.
        Also logs each page's URL (FAILURE_URL) -- used to build a direct "go to the page
        where this happened" link in the failure email for issues that need a person to
        resolve them (e.g. unmapped machines/products), rather than a blind auto-retry."""
        paths = []
        DEBUG_DIR.mkdir(exist_ok=True)
        for label, browser_ctx in (("micromart", self._micromart_ctx), ("vendsoft", self._vendsoft_ctx)):
            if browser_ctx is None:
                continue
            try:
                page = browser_ctx.pages[0] if browser_ctx.pages else None
                if page is None:
                    continue
                path = DEBUG_DIR / f"failure-{tag}-{label}.png"
                page.screenshot(path=str(path))
                paths.append(path)
                self.log.error("FAILURE_URL: %s %s", label, page.url)
            except Exception:
                pass
        return paths

    def close(self) -> None:
        for ctx in (self._micromart_ctx, self._vendsoft_ctx):
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass


def _submit_totp_code(page, secret: str, log: logging.Logger, site_label: str) -> bool:
    """Best-effort: find a 6-digit code input and submit a freshly computed TOTP code.
    Returns False (without error) if no such field is found or pyotp isn't installed,
    so the caller can fall back to the manual-intervention path."""
    try:
        import pyotp
    except ImportError:
        log.warning("pyotp not installed -- can't auto-submit TOTP. `pip install pyotp`.")
        return False

    selector = "input[autocomplete='one-time-code'], input[maxlength='6'], input[inputmode='numeric']"
    try:
        page.wait_for_selector(selector, timeout=8000, state="visible")
    except PlaywrightTimeoutError:
        log.info("%s: no TOTP field appeared within 8s", site_label)
        return False

    candidates = page.locator(selector)
    log.info("%s: TOTP field candidates found: %d", site_label, candidates.count())
    code_input = candidates.first

    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{site_label.lower()}-totp-01-field-found.png"))

    # Avoid submitting a code that's about to roll over -- network latency between filling
    # and the server validating it could otherwise turn a valid code into a rejected one,
    # and repeated rejections are exactly the kind of thing that gets an account locked.
    totp = pyotp.TOTP(secret)
    remaining = totp.interval - (time.time() % totp.interval)
    if remaining < 5:
        time.sleep(remaining + 0.5)
    code = totp.now()
    code_input.fill(code)
    page.screenshot(path=str(DEBUG_DIR / f"{site_label.lower()}-totp-02-filled.png"))

    submit = page.get_by_role("button", name=re.compile("verify|continue|submit|confirm", re.I))
    submit_count = submit.count()
    submit_labels = submit.all_inner_texts() if submit_count else []
    log.info("%s: TOTP submit button candidates: %s", site_label, submit_labels)
    if submit_count > 0:
        submit.first.click()
    else:
        page.keyboard.press("Enter")

    page.wait_for_timeout(800)
    page.screenshot(path=str(DEBUG_DIR / f"{site_label.lower()}-totp-03-after-click.png"))
    log.info("%s: submitted TOTP code automatically", site_label)
    return True


def _login_if_needed(
    log: logging.Logger,
    *,
    page_getter: Callable[[bool], object],
    dashboard_url: str,
    login_url_fragment: str,
    site_label: str,
    fill_fn: Callable[[object], None],
    submit_name: str,
    totp_keychain_service: Optional[str] = None,
) -> None:
    """page_getter(force_headed) returns the page to use -- called fresh each attempt so
    a headless failure can escalate to a headed browser for the retry (observed live:
    MicroMart blocks headless Chromium logins specifically; headed succeeds with the same
    credentials/TOTP). fill_fn takes the current page, since it may change across attempts."""
    forced_headed = False
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        page = page_getter(forced_headed)
        _goto(page, dashboard_url)
        _wait_settled(page)
        if login_url_fragment not in page.url:
            log.info("%s: already logged in (session reused)", site_label)
            return

        mode_note = " [headed escalation]" if forced_headed else ""
        log.info("%s: login required (attempt %d/%d)%s", site_label, attempt, MAX_LOGIN_ATTEMPTS,
                  mode_note)
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{site_label.lower()}-before-fill-attempt{attempt}.png"))
        try:
            fill_fn(page)
        except PlaywrightTimeoutError:
            page.screenshot(path=str(DEBUG_DIR / f"{site_label.lower()}-fill-timeout-attempt{attempt}.png"))
            raise
        page.get_by_role("button", name=re.compile(submit_name, re.I)).click()
        _wait_settled(page)

        if login_url_fragment not in page.url:
            log.info("%s: login succeeded", site_label)
            return

        DEBUG_DIR.mkdir(exist_ok=True)
        debug_shot = DEBUG_DIR / f"{site_label.lower()}-after-submit-attempt{attempt}.png"
        page.screenshot(path=str(debug_shot))
        log.info("%s: not yet past login after submit -- saved %s", site_label, debug_shot)

        if totp_keychain_service:
            secret = None
            secret_result = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ["USER"],
                 "-s", totp_keychain_service, "-w"],
                capture_output=True, text=True,
            )
            if secret_result.returncode == 0:
                secret = secret_result.stdout.strip()
            if secret and _submit_totp_code(page, secret, log, site_label):
                _wait_settled(page)
                if login_url_fragment not in page.url:
                    log.info("%s: login succeeded (TOTP auto-submitted)", site_label)
                    return

        for probe in OTP_PROBE_TEXTS:
            if page.get_by_text(re.compile(probe, re.I)).count() > 0:
                raise StepFailed(
                    f"{site_label} is asking for a one-time code and it couldn't be "
                    f"auto-submitted. Open the browser window this script controls "
                    f"(profile under browser-state/), complete it manually, then re-run "
                    f"with the same step -- the session will be saved for next time."
                )

        if login_url_fragment not in page.url:
            log.info("%s: login succeeded", site_label)
            return

        if HEADLESS and not forced_headed:
            log.warning("%s: headless login attempt failed -- escalating retry to headed mode",
                        site_label)
            forced_headed = True

    raise StepFailed(f"{site_label} login failed after {MAX_LOGIN_ATTEMPTS} attempts.")


# --- steps -------------------------------------------------------------

def step_micromart_login(ctx: Context) -> None:
    email = get_keychain_secret("micromart-platform-username")
    password = get_keychain_secret("micromart-platform")

    def fill(page):
        page.get_by_placeholder("name@example.com").fill(email)
        page.get_by_placeholder("Enter your password").fill(password)

    _login_if_needed(
        ctx.log,
        page_getter=ctx.micromart_page,
        dashboard_url=MICROMART_DASHBOARD,
        login_url_fragment=MICROMART_LOGIN_FRAGMENT,
        site_label="MicroMart",
        fill_fn=fill,
        submit_name="sign in",
        totp_keychain_service="micromart-platform-totp-secret",
    )


def _dismiss_hubspot_popup(page, log: logging.Logger) -> None:
    """MicroMart embeds HubSpot 'Web Interactives' marketing popups that can overlay
    the page and intercept clicks. Not part of the app itself -- just clear it out
    of the way if present."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        removed = page.evaluate(
            """() => {
                const selectors = [
                    '#hs-interactives-modal-overlay',
                    '#hs-web-interactives-top-anchor',
                    'iframe[title="Popup CTA"]',
                ];
                let count = 0;
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach((el) => { el.remove(); count++; });
                }
                return count;
            }"""
        )
        if removed:
            log.info("Removed %d HubSpot popup element(s)", removed)
    except Exception:
        pass


def step_micromart_filter_and_download(ctx: Context) -> None:
    # Deliberately no date filter: MicroMart excludes failed-payment rows from a day's
    # export entirely, and a retried payment that later succeeds keeps its *original*
    # transaction date rather than the retry date. A single-day pull would permanently
    # miss any transaction that failed and was retried on a later day. Pulling a rolling
    # 30-day "Last 30 Days" window every day instead means a newly-resolved retry gets
    # picked up within a day of becoming successful, and VendSoft's own per-transaction
    # duplicate detection (confirmed live -- it classifies New/Duplicate per row, not per
    # file) makes re-uploading the overlapping window safe on repeat.
    page = ctx.micromart_page()
    log = ctx.log

    _goto(page, MICROMART_TRANSACTIONS)
    _wait_settled(page)
    _dismiss_hubspot_popup(page, log)

    # Opens the Download CSV dropdown (trigger button, before the menu exists). Retry once
    # on a timeout -- confirmed live 2026-09-11, this click hung for the full 30s despite
    # the element reporting visible/enabled/stable right before, then succeeded immediately
    # on a fresh attempt (page reload + re-dismiss popup), consistent with a one-off flaky
    # paint/overlay rather than a real selector problem.
    for attempt in (1, 2):
        try:
            page.get_by_role("button", name=re.compile("Download CSV", re.I)).first.click()
            break
        except PlaywrightTimeoutError:
            if attempt == 2:
                raise
            log.warning("Download CSV button click timed out -- reloading and retrying once.")
            _goto(page, MICROMART_TRANSACTIONS)
            _wait_settled(page)
            _dismiss_hubspot_popup(page, log)

    # "Last 30 Days" is the modal's default when no date filter is applied -- click it
    # explicitly anyway rather than relying on that default holding.
    page.get_by_text(re.compile("^Last 30 Days$", re.I)).click()

    # Switch Report Type from the default "Transaction Summary" to "Itemized Sales".
    page.get_by_text("Itemized Sales", exact=False).click()

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    dest = TARGET_DIR / ctx.csv_filename

    # Second "Download CSV" button, now inside the open menu. A 30-day export can take a
    # minute or two to generate server-side before the download even starts, well past
    # Playwright's normal 30s default -- give it much more room.
    with page.expect_download(timeout=300000) as download_info:
        page.get_by_role("button", name=re.compile("Download CSV", re.I)).last.click()
    download = download_info.value
    download.save_as(str(dest))
    log.info("Saved itemized sales CSV to %s", dest)

    try:
        import csv as csv_module
        with open(dest, newline="", encoding="utf-8") as f:
            row_count = sum(1 for _ in csv_module.reader(f)) - 1  # exclude header
        log.info("CSV_ROW_COUNT: %d", row_count)
    except Exception as e:
        log.warning("Could not count CSV rows for reconciliation: %s", e)


def step_upload_to_drive(ctx: Context) -> None:
    from googleapiclient.http import MediaFileUpload

    log = ctx.log
    csv_path = TARGET_DIR / ctx.csv_filename
    if not csv_path.exists():
        raise StepFailed(f"Expected file not found: {csv_path}")

    if not DRIVE_FOLDER_ID_FILE.exists():
        raise StepFailed(
            f"No Drive folder ID configured. Put the target folder's ID (one line) in "
            f"{DRIVE_FOLDER_ID_FILE}."
        )
    folder_id = DRIVE_FOLDER_ID_FILE.read_text().strip()

    service = _drive_service()
    file_metadata = {"name": csv_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(csv_path), mimetype="text/csv")
    service.files().create(
        body=file_metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    log.info("Uploaded %s to Drive folder %s", csv_path.name, folder_id)


def step_vendsoft_login(ctx: Context) -> None:
    username = get_keychain_secret("vendsoft-next-username")
    password = get_keychain_secret("vendsoft-next")

    def fill(page):
        page.get_by_label("Email or username", exact=False).fill(username)
        page.get_by_label("Password", exact=False).fill(password)

    _login_if_needed(
        ctx.log,
        page_getter=ctx.vendsoft_page,
        dashboard_url=VENDSOFT_BASE,
        login_url_fragment=VENDSOFT_LOGIN_FRAGMENT,
        site_label="VendSoft",
        fill_fn=fill,
        submit_name="sign in",
    )


def step_vendsoft_import(ctx: Context) -> None:
    page = ctx.vendsoft_page()
    log = ctx.log

    csv_path = TARGET_DIR / ctx.csv_filename
    if not csv_path.exists():
        raise StepFailed(f"Expected file not found: {csv_path}")

    # Deep-linking straight to the import URL renders blank for this account (confirmed
    # live) -- the SPA's router apparently expects client-side navigation state that a
    # fresh page load doesn't have. Click through the sidebar instead, same as a human would.
    _goto(page, VENDSOFT_BASE)
    _wait_settled(page)
    page.get_by_text(re.compile("^Configuration$", re.I)).click()
    _wait_settled(page)
    page.get_by_text(re.compile("^TELEMETRY$", re.I)).click()
    _wait_settled(page)
    page.get_by_text(re.compile("^Sales import$", re.I)).click()
    _wait_settled(page)

    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / "vendsoft-sales-import-before-browse.png"))

    file_input = page.locator("input[type='file']")
    if file_input.count() > 0:
        file_input.first.set_input_files(str(csv_path))
    else:
        with page.expect_file_chooser() as fc_info:
            page.get_by_text(re.compile("BROWSE FILES", re.I)).click()
        fc_info.value.set_files(str(csv_path))

    log.info("Attached %s in VendSoft sales import", csv_path.name)

    _wait_settled(page)
    page.wait_for_timeout(3000)
    page.mouse.wheel(0, 400)
    page.wait_for_timeout(500)

    # VendSoft parses/dedupes the file server-side after attach (visible as a progress bar).
    # Wait for that to actually finish -- either the import button renders, or (for a batch
    # that turns out to be fully duplicate) it never does -- before reading any status text.
    # Reading too early can catch a mid-processing page: confirmed live 2026-09-10, an early
    # read matched a stray "already imported" label from the attempt-history list below the
    # fold while the current attempt's real result panel was still loading, causing the step
    # to report success without ever clicking the actual import button.
    import_button = page.get_by_role("button", name=re.compile(r"^IMPORT .*TRANSACTIONS$", re.I))
    try:
        import_button.first.wait_for(state="visible", timeout=60000)
    except PlaywrightTimeoutError:
        pass

    page.screenshot(path=str(DEBUG_DIR / "vendsoft-sales-import-after-attach.png"), full_page=True)

    # VendSoft's own docs describe two more required steps we haven't automated yet:
    # confirming machine/product mapping, then an explicit click to finalize the import.
    # Anything other than the two known-good outcomes below is unknown territory -- fail
    # safely and surface it, rather than silently reporting success on an import that may
    # not have actually completed (e.g. stuck on an unmapped machine/product screen we've
    # never seen and can't yet click through).
    try:
        page_text = page.locator("body").inner_text()
    except Exception as e:
        page_text = f"(could not capture page text: {e})"

    # Check the actionable "ready to import" screen FIRST. This must come before the
    # "already imported" text check below: VendSoft's page also lists a history of past
    # import attempts, and past entries there can legitimately be labeled "already imported"
    # even while the CURRENT attempt still has new transactions ready and an unclicked
    # import button -- a bare substring search over the whole page matches that history
    # text and returns early without ever importing anything (confirmed live 2026-09-10:
    # a batch with 9 ready transactions, 0 machines/products to review, and a visible
    # "IMPORT 9 TRANSACTIONS" button was wrongly reported as already_imported).
    machines_review = re.search(r"Machines to review\D*(\d+)", page_text)
    products_review = re.search(r"Products to review\D*(\d+)", page_text)

    if (
        machines_review and products_review
        and int(machines_review.group(1)) == 0
        and int(products_review.group(1)) == 0
        and import_button.count() > 0
    ):
        log.info("VendSoft review screen: everything auto-resolved (0 machines, 0 products "
                 "to review) -- clicking final import confirmation.")
        import_button.first.click()
        _wait_settled(page)
        page.wait_for_timeout(2000)
        page.screenshot(path=str(DEBUG_DIR / "vendsoft-sales-import-after-confirm.png"))
        try:
            post_text = page.locator("body").inner_text()
        except Exception as e:
            post_text = f"(could not capture post-import page text: {e})"
        stats = _extract_vendsoft_stats(post_text, outcome="imported")
        log.info("VENDSOFT_IMPORT_STATS: %s", json.dumps(stats))
        return

    # Only treat "already imported" as a terminal, nothing-to-do state once the actionable
    # path above has been ruled out -- and only when there's no live import button, as an
    # extra guard against the same history-text false positive.
    if re.search("already imported", page_text, re.I) and import_button.count() == 0:
        stats = _extract_vendsoft_stats(page_text, outcome="already_imported")
        log.info("VENDSOFT_IMPORT_STATS: %s", json.dumps(stats))
        return

    log.error("VendSoft did not show a known-good outcome after attaching (neither 'already "
              "imported' nor a fully auto-resolved review screen). Machines/products to review "
              "may be > 0, needing manual selection. Page text at this point: %s",
              page_text.replace("\n", " | "))
    raise StepFailed(
        "VendSoft is showing machines or products that need manual mapping (or an "
        "unrecognized screen) after attaching the file. Please open VendSoft, complete "
        "Sales Import -> mapping -> Import manually for this file, and send a screenshot "
        "of what appeared so this can be automated."
    )


STEPS = [
    ("micromart_login", step_micromart_login),
    ("micromart_filter_and_download", step_micromart_filter_and_download),
    ("upload_to_drive", step_upload_to_drive),
    ("vendsoft_login", step_vendsoft_login),
    ("vendsoft_import", step_vendsoft_import),
]


def run(target_date: date, resume_from: Optional[str]) -> int:
    log_file = LOG_DIR / f"{target_date.strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("sync")
    ctx = Context(target_date=target_date, log=log)

    names = [name for name, _ in STEPS]
    if resume_from and resume_from not in names:
        log.error("Unknown --resume-from step %r. Valid steps: %s", resume_from, names)
        return 2
    start_index = names.index(resume_from) if resume_from else 0

    try:
        for name, fn in STEPS[start_index:]:
            log.info("=== step: %s ===", name)
            try:
                fn(ctx)
            except StepFailed as e:
                log.error("Step '%s' failed: %s", name, e)
                for path in ctx.screenshot_all(name):
                    log.error("FAILURE_SCREENSHOT: %s", path)
                log.error("Fix the issue, then resume with: --resume-from %s --date %s",
                          name, ctx.date_dashed)
                return 1
            except NotImplementedError as e:
                log.warning("Step '%s' not yet implemented: %s", name, e)
                return 1
            except Exception:
                log.exception("Step '%s' crashed unexpectedly", name)
                for path in ctx.screenshot_all(f"{name}-crash"):
                    log.error("FAILURE_SCREENSHOT: %s", path)
                log.error("Fix the issue, then resume with: --resume-from %s --date %s",
                          name, ctx.date_dashed)
                return 1
        log.info("All steps completed for %s", ctx.date_dashed)
        return 0
    finally:
        ctx.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="MM-DD-YYYY, label date for output files; defaults to today",
                         default=None)
    parser.add_argument("--resume-from", help="step name to resume from", default=None)
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%m-%d-%Y").date()
    else:
        target_date = date.today()

    sys.exit(run(target_date, args.resume_from))


if __name__ == "__main__":
    main()
