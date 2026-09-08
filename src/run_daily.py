#!/usr/bin/env python3
"""
Entry point for the scheduled daily run: executes the rolling 30-day sync, then emails a
success or failure report. This is what the launchd job calls.

Manual equivalent of running sync.py directly, but with reporting on top.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import email_report
import sync


def _last_match(pattern: str, text: str):
    matches = re.findall(pattern, text, re.MULTILINE)
    return matches[-1] if matches else None


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_KEEP_MARKERS = ("[ERROR]", "[WARNING]", "=== step:", "FAILURE_SCREENSHOT")


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
    target_date = date.today()
    date_str = target_date.strftime("%m-%d-%Y")
    csv_filename = f"transaction-items-last30days-{date_str}.csv"

    crash_message = None
    try:
        exit_code = sync.run(target_date, resume_from=None)
    except Exception as e:
        exit_code = 1
        crash_message = f"{type(e).__name__}: {e}"

    log_file = sync.LOG_DIR / f"{target_date.strftime('%Y-%m-%d')}.log"
    log_text = log_file.read_text() if log_file.exists() else ""

    if exit_code == 0 and crash_message is None:
        stats_raw = _last_match(r"VENDSOFT_IMPORT_STATS: (.+)", log_text)
        stats = json.loads(stats_raw) if stats_raw else {"outcome": None}
        csv_row_count_raw = _last_match(r"CSV_ROW_COUNT: (\d+)", log_text)
        csv_row_count = int(csv_row_count_raw) if csv_row_count_raw else None

        html = email_report.render_success_html(date_str, csv_filename, csv_row_count, stats)
        email_report.send_email(f"MicroMart sync succeeded - {date_str}", html)
    else:
        failed_step = _last_match(r"=== step: (\S+) ===", log_text) or "(unknown)"
        error_message = crash_message or _last_match(r"\[ERROR\] (.+)", log_text) or "(see log for details)"
        resume_hint = _last_match(r"resume with: (.+)", log_text) or \
            f"--resume-from {failed_step} --date {date_str}"
        failed_at = _last_match(r"^(\S+ \S+) \[ERROR\]", log_text) or "(unknown time)"
        screenshot_paths = re.findall(r"FAILURE_SCREENSHOT: (.+)", log_text)

        html = email_report.render_failure_html(
            date_str, failed_step, error_message, resume_hint,
            failed_at=failed_at, log_excerpt=_important_log_lines(log_text),
            screenshot_count=len(screenshot_paths),
        )
        email_report.send_email(
            f"ACTION NEEDED: MicroMart sync failed - {date_str}", html, image_paths=screenshot_paths,
        )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
