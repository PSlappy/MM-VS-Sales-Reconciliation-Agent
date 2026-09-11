# Retry-trigger setup (the "Retry" button in failure emails)

This lets you or your business partner click a button in a failure email to have the sync
retried, without needing terminal access to the Mac. It's a one-time setup with a few pieces:

- A small Google Apps Script Web App (`apps-script/retry-webapp.gs`) — deployed under the same
  `info@accessamenities.com` Google account, this is the only part of the system that's
  actually reachable from the internet. It just holds a flag.
- A secret token shared between that Web App and this Mac's Keychain, so nobody else can
  trigger a retry just by guessing the URL.
- A new `launchd` job (`check_retry_trigger.py`, every 5 minutes) that polls the Web App and
  acts on the flag. It only makes that network call at all on a day where today's run hit a
  transient failure and hasn't succeeded yet — an ordinary successful day costs nothing.

**Why a Web App at all, and not something simpler**: this Mac has no public address, and
opening it up to receive inbound clicks directly would mean port forwarding or a dynamic-DNS
setup — a bigger and riskier step than we've taken so far, and a different thing entirely from
the cloud-hosting question that's already tabled (see the README's "Future: hosting beyond this
Mac" section). The Web App is Google-hosted, free, and the Mac only ever calls out to it —
never the other way around. Same trust shape as the Drive/Gmail API calls the tool already
makes.

**What this does and doesn't solve**: it only helps once the Mac is on and awake and something
recoverable went wrong (a timeout, a flaky click — the retry email fixed on 2026-09-11 is a
good example). If the Mac is fully shut down, nothing here can wake it, same as before. And a
failure that needs a person — like an unmapped machine or product — gets a different email
entirely, with a link straight to the page it happened on instead of a retry button, since
retrying that automatically would just fail the same way again.

## 1. Deploy the Apps Script Web App

1. Go to https://script.google.com, signed in as `info@accessamenities.com`.
2. **New project**. Name it something like `mm-vs-retry-webapp`.
3. Delete the default `Code.gs` contents and paste in the contents of
   `apps-script/retry-webapp.gs` from this repo.
4. **Project Settings** (gear icon) → **Script Properties** → **Add script property**:
   - Property: `SECRET`
   - Value: *(leave this tab open — you'll generate and paste this in step 2 below)*
5. **Deploy → New deployment**:
   - Type: **Web app**
   - Execute as: **Me** (`info@accessamenities.com`)
   - Who has access: **Anyone**
6. Click **Deploy**, authorize the requested permissions, and copy the **Web app URL**
   (looks like `https://script.google.com/macros/s/.../exec`).

   **Heads up**: if your Workspace's sharing policy is locked down the same way service-account
   key creation was, "Anyone" might not be selectable and you'll only be able to choose "Anyone
   within Access Amenities" — if so, that's fine, it just means your business partner needs to
   be signed into a `@accessamenities.com` Google account (or whichever your Workspace domain
   is) in the browser tab where they click the retry link. Let this doc know if you hit that so
   it can be updated with the exact wording you saw.

## 2. Generate the shared secret

Run this on the Mac — it generates a random token, stores it directly in Keychain, and copies
it to your clipboard without it ever appearing in a chat or a file:

```bash
SECRET=$(openssl rand -base64 32 | tr -d '\n')
security add-generic-password -a "$USER" -s "mm-vs-retry-webapp-secret" -w "$SECRET" -U
echo "$SECRET" | pbcopy
echo "Secret generated and copied to your clipboard. Paste it into the Apps Script SECRET property now."
unset SECRET
```

Go back to the Apps Script tab from step 1.4 and paste the clipboard contents into the
`SECRET` property's value field, then **Save**.

## 3. Point the Mac at the deployed Web App

Save the Web app URL from step 1.6 into:

```
micromart-vendsoft-sync/config/retry-webapp-url.txt
```

(a single line, just the URL — this file is gitignored, same as `drive-folder-id.txt`).

## 4. Install the polling launchd job

```bash
cp /Users/patrickshea/github/access-amenities-tools/micromart-vendsoft-sync/launchd/com.accessamenities.micromart-vendsoft-retry-poller.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.accessamenities.micromart-vendsoft-retry-poller.plist
```

## 5. Test it end to end

1. Force a failure on purpose isn't necessary — instead, sanity-check the pieces individually:
   - Visit the Web app URL directly in a browser with `?action=retry&token=<your secret>&date=09-11-2026` appended. You should see the "Retry the MicroMart → VendSoft sync?" confirmation page. Click **Confirm Retry** — you should see "Retry requested."
   - Run `.venv/bin/python src/check_retry_trigger.py` by hand. If today's log doesn't show a transient failure yet, it'll exit silently (that's correct — it's supposed to stay quiet). To actually see it act, you'd need a real transient-failure day, or a manually-edited test log — not worth forcing.
2. Next time a real transient failure happens, the failure email will include a **Retry Now**
   button. Confirm it. Within ~5 minutes (or immediately at next login/boot), the poller should
   pick it up, resume the sync from the failed step, and send a fresh success/failure email.

## Notes

- The `SECRET` value only needs to be set in two places: the Apps Script property, and this
  Mac's Keychain. If either is ever regenerated, update both to match.
- The retry flag is scoped to a specific date (`MM-DD-YYYY`) and is consumed the moment it's
  read, so an old retry link from a past day's email is harmless if someone clicks it later —
  the poller only ever checks *today's* date.
- If you ever want to revoke this entirely, delete the Apps Script deployment (or just remove
  the `SECRET` script property) and unload the poller LaunchAgent
  (`launchctl unload ~/Library/LaunchAgents/com.accessamenities.micromart-vendsoft-retry-poller.plist`).
