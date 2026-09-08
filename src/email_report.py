#!/usr/bin/env python3
"""HTML email reports for the MicroMart -> VendSoft daily sync, sent via Gmail
API as info@accessamenities.com. Simple templates for now -- meant to be
refined over time rather than gotten perfect up front."""
import base64
import html
import os
import subprocess
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OAUTH_CLIENT_FILE = ROOT / "config" / "oauth-client.json"
RECIPIENTS_FILE = ROOT / "config" / "notify-recipients.txt"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
KEYCHAIN_SERVICE = "vendsoft-drive-oauth-refresh-token"
SENDER = "info@accessamenities.com"


def get_recipients() -> list:
    if not RECIPIENTS_FILE.exists():
        raise RuntimeError(f"No recipients configured. Add one email per line to {RECIPIENTS_FILE}")
    return [line.strip() for line in RECIPIENTS_FILE.read_text().splitlines() if line.strip()]


def _keychain_secret(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", os.environ["USER"], "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read '{service}' from Keychain.")
    return result.stdout.strip()


def _gmail_service():
    import json
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    client_config = json.loads(OAUTH_CLIENT_FILE.read_text())["installed"]
    refresh_token = _keychain_secret(KEYCHAIN_SERVICE)
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds)


def send_email(subject: str, html_body: str, image_paths: list = None) -> None:
    to_list = get_recipients()

    if image_paths:
        message = MIMEMultipart("related")
        message.attach(MIMEText(html_body, "html"))
        for i, path in enumerate(image_paths):
            path = Path(path)
            if not path.exists():
                continue
            img = MIMEImage(path.read_bytes())
            img.add_header("Content-ID", f"<shot{i}>")
            img.add_header("Content-Disposition", "inline", filename=path.name)
            message.attach(img)
    else:
        message = MIMEText(html_body, "html")

    message["to"] = ", ".join(to_list)
    message["from"] = SENDER
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service = _gmail_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def _wrap(accent_color: str, title: str, body_html: str) -> str:
    return f"""\
<div style="font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;background:#f4f5f7;padding:24px;">
  <div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
    <div style="background:{accent_color};padding:16px 24px;">
      <h1 style="margin:0;color:#ffffff;font-size:18px;">{html.escape(title)}</h1>
    </div>
    <div style="padding:24px;color:#1f2937;font-size:14px;line-height:1.6;">
      {body_html}
    </div>
    <div style="padding:16px 24px;background:#f9fafb;color:#9ca3af;font-size:12px;">
      MicroMart &rarr; VendSoft daily sync &middot; Access Amenities
    </div>
  </div>
</div>"""


_OUTCOME_LABELS = {
    "already_imported": "Already imported (exact duplicate)",
    "imported": "Imported",
}


def _stat_row(label: str, value) -> str:
    display = "—" if value is None else html.escape(str(value))
    return (f'<tr><td style="padding:6px 0;color:#6b7280;width:180px;vertical-align:top;">'
            f'{html.escape(label)}</td><td style="padding:6px 0;">{display}</td></tr>')


def render_success_html(date_str: str, csv_filename: str, csv_row_count, stats: dict) -> str:
    outcome = stats.get("outcome")
    vendsoft_rows = stats.get("rows")

    if csv_row_count is not None and vendsoft_rows is not None:
        if csv_row_count == vendsoft_rows:
            reconciliation = (f'<span style="color:#16a34a;">&#10003; Match</span> '
                               f'&mdash; {csv_row_count} rows in the downloaded CSV, '
                               f'VendSoft parsed the same {vendsoft_rows}.')
        else:
            reconciliation = (f'<span style="color:#dc2626;font-weight:bold;">&#9888; Mismatch</span> '
                               f'&mdash; downloaded CSV had {csv_row_count} rows, but VendSoft '
                               f'reported parsing {vendsoft_rows}. Worth a look.')
    else:
        reconciliation = "— (row counts unavailable for comparison)"

    # This is a rolling 30-day export, so most transactions in any given file are expected
    # to already be imported from previous days -- materialized < txns is the normal case,
    # not a problem. This line is purely informational, not a pass/fail check like the row
    # count reconciliation above (which is about parsing integrity, not date overlap).
    txns = stats.get("txns")
    materialized = stats.get("imported")
    materialized_check = None
    if outcome == "imported" and txns is not None and materialized is not None:
        already_seen = txns - materialized
        if already_seen == 0:
            materialized_check = f"All {txns} transactions in this file were newly materialized."
        elif already_seen > 0:
            materialized_check = (f"{materialized} of {txns} transactions in this file were newly "
                                   f"materialized ({already_seen} were already-imported duplicates "
                                   f"from previous days &mdash; expected for a rolling 30-day pull).")
        else:
            materialized_check = (f'<span style="color:#dc2626;font-weight:bold;">&#9888; Unexpected</span> '
                                   f'&mdash; {materialized} materialized is more than the {txns} '
                                   f'transactions in the file. Worth a look.')

    rows = [_stat_row("File", csv_filename)]
    rows.append(_stat_row("Outcome", _OUTCOME_LABELS.get(outcome, outcome or "unknown")))
    rows.append(_stat_row("Rows (VendSoft parsed)", vendsoft_rows))
    rows.append(_stat_row("Transactions", txns))
    rows.append(_stat_row("Products", stats.get("products")))
    rows.append(_stat_row("Machines matched", stats.get("machines_matched")))
    if outcome == "already_imported":
        rows.append(_stat_row("Exact duplicates", stats.get("duplicates")))
    elif outcome == "imported":
        rows.append(_stat_row("Transactions materialized", materialized))

    checks_html = f"""\
    <p style="background:#f9fafb;border-radius:6px;padding:10px 12px;font-size:13px;">
      <strong>CSV vs VendSoft row count:</strong><br>{reconciliation}
    </p>"""
    if materialized_check:
        checks_html += f"""\
    <p style="background:#f9fafb;border-radius:6px;padding:10px 12px;font-size:13px;margin-top:8px;">
      <strong>New vs. already-imported:</strong><br>{materialized_check}
    </p>"""

    body = f"""\
    <p>The daily MicroMart &rarr; VendSoft sync completed successfully for <strong>{html.escape(date_str)}</strong>.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      {"".join(rows)}
    </table>
    {checks_html}
    <p style="color:#6b7280;font-size:13px;">No action needed.</p>
    """
    return _wrap("#16a34a", f"Sync succeeded — {date_str}", body)


def render_failure_html(
    date_str: str,
    failed_step: str,
    error_message: str,
    resume_hint: str,
    failed_at: str,
    log_excerpt: str,
    screenshot_count: int = 0,
) -> str:
    screenshots_html = ""
    if screenshot_count:
        imgs = "".join(
            f'<img src="cid:shot{i}" style="max-width:100%;border:1px solid #e5e7eb;border-radius:6px;margin:8px 0;display:block;">'
            for i in range(screenshot_count)
        )
        screenshots_html = f"""\
        <p><strong>Screen at the moment of failure:</strong></p>
        {imgs}
        """

    body = f"""\
    <p>The daily MicroMart &rarr; VendSoft sync <strong>failed</strong> for <strong>{html.escape(date_str)}</strong>
       and has stopped &mdash; it will not keep retrying on its own beyond the built-in attempts.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr><td style="padding:6px 0;color:#6b7280;width:140px;vertical-align:top;">Failed at</td>
          <td style="padding:6px 0;">{html.escape(failed_at)}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;vertical-align:top;">Step / stage</td>
          <td style="padding:6px 0;">{html.escape(failed_step)}</td></tr>
      <tr><td style="padding:6px 0;color:#6b7280;vertical-align:top;">Why</td>
          <td style="padding:6px 0;">{html.escape(error_message)}</td></tr>
    </table>
    {screenshots_html}
    <p><strong>Action needed:</strong> fix the issue, then resume with:</p>
    <pre style="background:#f3f4f6;padding:12px;border-radius:6px;overflow-x:auto;font-size:12px;">{html.escape(resume_hint)}</pre>
    <p style="margin-top:20px;"><strong>Full run log:</strong></p>
    <pre style="background:#111827;color:#e5e7eb;padding:12px;border-radius:6px;overflow-x:auto;font-size:11px;max-height:400px;">{html.escape(log_excerpt)}</pre>
    """
    return _wrap("#dc2626", f"ACTION NEEDED — sync failed — {date_str}", body)
