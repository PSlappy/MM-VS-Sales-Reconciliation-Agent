// Retry trigger receiver for MM-VS-Sales-Reconciliation-Agent's failure emails.
//
// This isn't part of the Python tool -- it's pasted into a separate Google Apps Script
// project and deployed as a Web App, by hand, one time. See docs/retry-trigger-setup.md in
// this repo for the full walkthrough. It exists only because the Mac that actually runs the
// sync can't receive inbound clicks from the internet; this is the small piece that can,
// under the same Google account, and the Mac polls it on its own schedule instead.
//
// Required one-time config in this Apps Script project:
//   Project Settings -> Script Properties -> add a property named SECRET, set to a long
//   random value that matches exactly what's stored in the Mac's Keychain under the service
//   name "mm-vs-retry-webapp-secret".

function _secret() {
  return PropertiesService.getScriptProperties().getProperty('SECRET');
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
}

function _html(body) {
  return HtmlService.createHtmlOutput(body);
}

function _escape(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

// Only ever "MM-DD-YYYY" (see sync.py's date_dashed) -- reject anything else outright rather
// than echoing an unvalidated query param back into HTML or a Script Properties key.
function _validDate(d) {
  return /^\d{2}-\d{2}-\d{4}$/.test(d);
}

function doGet(e) {
  const action = e.parameter.action;
  const token = e.parameter.token;
  const date = e.parameter.date;

  if (!token || token !== _secret()) {
    return _html('<p>Invalid or expired link.</p>');
  }
  if (!_validDate(date)) {
    return _html('<p>Invalid link.</p>');
  }

  if (action === 'retry') {
    // Deliberately a confirmation page, not an instant side effect -- email link-scanners
    // and prefetchers sometimes fetch links automatically just from the email being opened.
    // The actual flag only gets set by the doPost below, from a real click on this button.
    return _html(
      '<html><body style="font-family:sans-serif;text-align:center;padding:60px 20px;">' +
      '<h2>Retry the MicroMart &rarr; VendSoft sync?</h2>' +
      '<p>For ' + _escape(date) + '</p>' +
      '<form method="POST" action="' + ScriptApp.getService().getUrl() + '">' +
      '<input type="hidden" name="action" value="confirm-retry">' +
      '<input type="hidden" name="token" value="' + _escape(token) + '">' +
      '<input type="hidden" name="date" value="' + _escape(date) + '">' +
      '<button type="submit" style="font-size:16px;padding:10px 24px;background:#2563eb;' +
      'color:#fff;border:none;border-radius:6px;cursor:pointer;">Confirm Retry</button>' +
      '</form>' +
      '<p style="color:#6b7280;font-size:13px;margin-top:20px;">' +
      'Picked up within about 5 minutes, only if the Mac is on and awake.</p>' +
      '</body></html>'
    );
  }

  if (action === 'check') {
    const props = PropertiesService.getScriptProperties();
    const key = 'retry_' + date;
    const requested = props.getProperty(key) === '1';
    if (requested) props.deleteProperty(key);  // consume-on-read, so it only fires once
    return _json({ retry_requested: requested });
  }

  return _json({ error: 'unknown action' });
}

function doPost(e) {
  const token = e.parameter.token;
  const date = e.parameter.date;
  const action = e.parameter.action;

  if (!token || token !== _secret()) {
    return _html('<p>Invalid or expired link.</p>');
  }
  if (!_validDate(date)) {
    return _html('<p>Invalid link.</p>');
  }

  if (action === 'confirm-retry') {
    PropertiesService.getScriptProperties().setProperty('retry_' + date, '1');
    return _html(
      '<html><body style="font-family:sans-serif;text-align:center;padding:60px 20px;">' +
      '<h2>Retry requested.</h2>' +
      '<p>The Mac will pick this up within about 5 minutes if it is on and awake.</p>' +
      '</body></html>'
    );
  }

  return _json({ error: 'unknown action' });
}
