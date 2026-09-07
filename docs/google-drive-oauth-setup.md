# Google Drive OAuth setup (for info@accessamenities.com)

Your organization blocks service account key downloads (`iam.managed.disableServiceAccountKeyCreation`)
— a sensible security policy, so we're not fighting it. Instead the script authenticates as
`info@accessamenities.com` directly via OAuth, the same way a desktop app like Backup and Sync
does. One-time interactive consent, then a refresh token sits in Keychain and it's silent after
that. No service account, no key file, no folder-sharing step needed — uploads use whatever
Drive access `info@accessamenities.com` already has.

You can delete the `vendsoft-micromart-sales-sync` service account from IAM if you already
created one — it won't be used.

## 1. Enable the Drive API (skip if already done)

1. https://console.cloud.google.com/ — same project as before, or a new one, signed in as
   `info@accessamenities.com`.
2. **APIs & Services → Library → Google Drive API → Enable**.

## 2. Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. User type: **Internal** if this is a Google Workspace org (recommended — no Google review
   needed, only your org's users can consent) or **External** if it's a personal/consumer
   account.
3. Fill in an app name (e.g. "Access Amenities Sales Sync") and your email as support/developer
   contact. Save.
4. If User type is **External**, add `info@accessamenities.com` under **Test users** — otherwise
   the consent screen will refuse to load for it.

## 3. Create the OAuth client

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**.
3. Name it e.g. `micromart-vendsoft-sync-desktop`.
4. Download the resulting JSON (button looks like "Download JSON"). Save it to:
   ```
   micromart-vendsoft-sync/config/oauth-client.json
   ```
   This path is already in `.gitignore`. This file only identifies the *app* — it is not a
   credential for your Drive data by itself, so it's lower-risk than a service account key, but
   still keep it out of git.

## 4. Do the one-time consent (I'll drive this once the client exists)

Once `oauth-client.json` is in place, I'll write a small one-time script that:
1. Opens a browser window to Google's consent screen.
2. You sign in as `info@accessamenities.com` and approve Drive access (scoped to
   `drive.file`, meaning it can only see/write files it creates or that are explicitly shared
   with it — not your whole Drive).
3. Saves the resulting refresh token into Keychain (`vendsoft-drive-oauth-token`), same pattern
   as the other credentials.

After that, the daily sync script reads the refresh token from Keychain and silently gets a
fresh access token each run — no browser, no prompts.

## 5. Confirm the folder ID

Open `Shared drives → Access Amenities → VendSoft → Sales-Import → MicroMart-Transactions-Itemized-Sales`
in Drive and copy the ID from the URL (`.../folders/<FOLDER_ID>`) — needed for the upload call.

---

Let me know once steps 1–3 are done (client JSON in place) and I'll build and run the one-time
consent script with you, then wire up the actual upload step.
