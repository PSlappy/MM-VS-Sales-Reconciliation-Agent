#!/usr/bin/env python3
"""
Entry point for the scheduled daily run: executes the rolling 30-day sync, then emails a
success or failure report. This is what the launchd job calls.

Manual equivalent of running sync.py directly, but with reporting on top.

--resume-from is used two ways: by hand after fixing something, and by check_retry_trigger.py
when someone clicks "Retry" in a failure email (see docs/retry-trigger-setup.md). Passing it
skips the "already completed today" idempotency short-circuit below, since resuming is only
ever requested when today's run is known to still be unfinished.
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import email_report
import retry_trigger
import sync

# Which browser's FAILURE_URL to surface in a "go to the page where this happened" link --
# keyed by step name prefix, since both browser contexts can still be open (and screenshotted)
# regardless of which one the failed step actually belongs to.
_STEP_SITE = {
    "micromart_login": "micromart",
    "micromart_filter_and_download": "micromart",
    "upload_to_drive": None,  # a Drive API call, not a browser page
    "vendsoft_login": "vendsoft",
    "vendsoft_import": "vendsoft",
}


def _last_match(pattern: str, text: str):
    matches = re.findall(pattern, text, re.MULTILINE)
    return matches[-1] if matches else None


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_KEEP_MARKERS = ("[ERROR]", "[WARNING]", "=== step:", "FAILURE_SCREENSHOT",
                  "PRODUCT_AUTO_RESOLVED", "PRODUCT_MAPPING_NEEDS_REVIEW")


def _important_log_lines(log_text: str, max_lines: int = 50) -> str:
    """Cap the log excerpt to step markers, warnings/errors (with their full
    traceback continuation lines), and screenshot references -- not the whole log."""
    lines = log_text.splitlines()
    important = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(marker in line for marker in _KEEP_MARKERS):
            important.append(line)
            i += 1
            while i < len(lines) and not _TIMESTAMP.match(lines[i]):
                important.append(lines[i])
                i += 1
        else:
            i += 1
    if not important:
        return "(no notable log lines found)"
    if len(important) > max_lines:
        omitted = len(lines) - max_lines
        important = important[-max_lines:]
        important.insert(0, f"... ({omitted} earlier lines omitted) ...")
    return "\n".join(important)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-from", help="step name to resume from", default=None)
    args = parser.parse_args()

    target_date = date.today()
    date_str = target_date.strftime("%m-%d-%Y")
    csv_filename = f"transaction-items-last30days-{date_str}.csv"

    # Runs at both the 8 AM calendar trigger AND at login/boot (RunAtLoad), so a login-time
    # catch-up run actually happens if the Mac was fully off (not just asleep) at 8 AM --
    # launchd's calendar-interval catch-up only covers sleep, not a real shutdown. Skip if
    # today's sync already completed successfully, so an ordinary 8-AM-awake day plus a later
    # login/reboot doesn't trigger a duplicate run and a duplicate email. A --resume-from call
    # (manual, or from check_retry_trigger.py) always proceeds -- it's only ever made when
    # today is already known to be unfinished.
    log_file = sync.LOG_DIR / f"{target_date.strftime('%Y-%m-%d')}.log"
    if args.resume_from is None and log_file.exists() and "All steps completed" in log_file.read_text():
        print(f"{date_str} already completed successfully earlier today -- skipping.")
        sys.exit(0)

    crash_message = None
    try:
        exit_code = sync.run(target_date, resume_from=args.resume_from)
    except Exception as e:
        exit_code = 1
        crash_message = f"{type(e).__name__}: {e}"

    log_text = log_file.read_text() if log_file.exists() else ""

    if exit_code == 0 and crash_message is None:
        stats_raw = _last_match(r"VENDSOFT_IMPORT_STATS: (.+)", log_text)
        stats = json.loads(stats_raw) if stats_raw else {"outcome": None}
        csv_row_count_raw = _last_match(r"CSV_ROW_COUNT: (\d+)", log_text)
        csv_row_count = int(csv_row_count_raw) if csv_row_count_raw else None
        resolution_raw = _last_match(r"PRODUCT_AUTO_RESOLVED: (.+)", log_text)
        product_resolution = json.loads(resolution_raw) if resolution_raw else None

        html = email_report.render_success_html(
            date_str, csv_filename, csv_row_count, stats, product_resolution=product_resolution,
        )
        email_report.send_email(f"MicroMart sync succeeded - {date_str}", html)
    else:
        failed_step = _last_match(r"=== step: (\S+) ===", log_text) or "(unknown)"
        error_message = crash_message or _last_match(r"\[ERROR\] (.+)", log_text) or "(see log for details)"
        resume_hint = _last_match(r"resume with: (.+)", log_text) or \
            f"--resume-from {failed_step} --date {date_str}"
        failed_at = _last_match(r"^(\S+ \S+) \[ERROR\]", log_text) or "(unknown time)"
        screenshot_paths = re.findall(r"FAILURE_SCREENSHOT: (.+)", log_text)
        site = _STEP_SITE.get(failed_step)
        page_link = None
        if site:
            page_link = _last_match(rf"FAILURE_URL: {re.escape(site)} (\S+)", log_text)

        needs_review_raw = _last_match(r"PRODUCT_MAPPING_NEEDS_REVIEW: (.+)", log_text)

        if needs_review_raw:
            # A third outcome, distinct from both transient and deliberate failures below:
            # nothing's broken, a person just needs to make a call the matching logic wasn't
            # confident enough to make. A retry here IS worth offering (unlike a plain
            # StepFailed) -- once the mapping is fixed by hand in VendSoft, resuming just
            # re-attaches the same file and re-checks, no repeated login risk involved.
            resolution = json.loads(needs_review_raw)
            retry_link = None
            try:
                retry_link = retry_trigger.build_retry_link(date_str)
            except Exception as e:
                print(f"Could not build retry link (retry trigger not set up yet?): {e}", file=sys.stderr)

            html = email_report.render_product_review_html(
                date_str, resolution, page_link=page_link, retry_link=retry_link,
                screenshot_count=len(screenshot_paths), log_excerpt=_important_log_lines(log_text),
            )
            email_report.send_email(
                f"Product mapping needs a look - MicroMart sync - {date_str}", html,
                image_paths=screenshot_paths,
            )
        else:
            # Two different failure shapes, matching sync.py's own two except branches: an
            # unexpected crash (transient -- a timeout, a network blip) is safe to retry
            # automatically, while any other deliberate StepFailed will just fail the exact
            # same way again until a person resolves it in the actual site -- retrying it
            # automatically would only waste an unattended login attempt for nothing (and
            # repeated login churn is exactly what caused a real MFA lockout during
            # development). So: transient gets a Retry button; deliberate gets a link to the
            # page where it happened instead, with no retry offered.
            is_transient = "crashed unexpectedly" in log_text

            retry_link = None
            manual_action_link = None
            if is_transient:
                try:
                    retry_link = retry_trigger.build_retry_link(date_str)
                except Exception as e:
                    print(f"Could not build retry link (retry trigger not set up yet?): {e}", file=sys.stderr)
            else:
                manual_action_link = page_link

            html = email_report.render_failure_html(
                date_str, failed_step, error_message, resume_hint,
                failed_at=failed_at, log_excerpt=_important_log_lines(log_text),
                screenshot_count=len(screenshot_paths),
                is_transient=is_transient, retry_link=retry_link, manual_action_link=manual_action_link,
            )
            email_report.send_email(
                f"ACTION NEEDED: MicroMart sync failed - {date_str}", html, image_paths=screenshot_paths,
            )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
