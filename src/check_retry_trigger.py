#!/usr/bin/env python3
"""
Runs every 5 minutes via launchd (launchd/com.accessamenities.micromart-vendsoft-retry-poller.plist)
to pick up a "Retry" click from a failure email -- see docs/retry-trigger-setup.md for the full
mechanism. Deliberately cheap on every ordinary run: it only makes a network call at all on a
day where today's log shows a transient (auto-retryable) failure and nothing has completed yet.
On any other day -- success, still running, no run yet, or a deliberate StepFailed that needs a
person instead -- it reads one local log file and exits immediately.
"""
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_trigger
import sync


def main() -> None:
    target_date = date.today()
    date_str = target_date.strftime("%m-%d-%Y")
    log_file = sync.LOG_DIR / f"{target_date.strftime('%Y-%m-%d')}.log"

    if not log_file.exists():
        return  # nothing has run yet today
    log_text = log_file.read_text()

    if "All steps completed" in log_text:
        return  # today already succeeded -- nothing to retry
    if "crashed unexpectedly" not in log_text:
        return  # no transient failure today (either still mid-run, or a deliberate StepFailed
                 # that needs a person, not a retry) -- stay quiet, no network call

    try:
        retry_wanted = retry_trigger.check_and_clear(date_str)
    except Exception as e:
        print(f"Could not reach the retry Web App: {e}", file=sys.stderr)
        return

    if not retry_wanted:
        return

    failed_step = None
    for line in reversed(log_text.splitlines()):
        if "resume with: --resume-from" in line:
            failed_step = line.split("--resume-from", 1)[1].split("--date")[0].strip()
            break

    print(f"Retry requested via email for {date_str} -- resuming from step: {failed_step or '(unknown)'}")
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "run_daily.py")]
    if failed_step:
        cmd += ["--resume-from", failed_step]
    subprocess.run(cmd, cwd=str(sync.ROOT), env=os.environ.copy(), check=False)


if __name__ == "__main__":
    main()
