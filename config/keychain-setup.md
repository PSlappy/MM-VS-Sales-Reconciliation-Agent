# Storing credentials in macOS Keychain

Run these yourself in Terminal — passwords never pass through Claude or this chat. Each
command below prompts you to type the value with **no echo** (nothing shows as you type,
like a `sudo` prompt) and never puts it into your shell history or the command line itself —
which also sidesteps the `dquote>` problem you'd hit if a real password contains a `"`, `` ` ``,
`$`, or similar shell-special character.

```bash
printf "MicroMart password: " && read -rs PW && echo && security add-generic-password -a "$USER" -s "micromart-platform" -w "$PW" -U && unset PW
```

```bash
printf "MicroMart username/email: " && read -rs PW && echo && security add-generic-password -a "$USER" -s "micromart-platform-username" -w "$PW" -U && unset PW
```

```bash
printf "VendSoft password: " && read -rs PW && echo && security add-generic-password -a "$USER" -s "vendsoft-next" -w "$PW" -U && unset PW
```

```bash
printf "VendSoft username/email: " && read -rs PW && echo && security add-generic-password -a "$USER" -s "vendsoft-next-username" -w "$PW" -U && unset PW
```

## MFA (TOTP secret)

MicroMart's `automatons@accessamenities.com` account requires MFA. Using an authenticator
app (not SMS) means the script can compute valid codes itself — see the main README for how
to get the raw secret key during setup. Store it the same way:

```bash
printf "MicroMart TOTP secret: " && read -rs PW && echo && security add-generic-password -a "$USER" -s "micromart-platform-totp-secret" -w "$PW" -U && unset PW
```

MicroMart also issues a one-time **recovery code** when MFA is enabled -- a human-only fallback
for if the TOTP secret is ever lost, not something the script uses day to day. Its primary,
durable home should be Bitwarden (or written down) since it needs to survive this Mac being
unavailable; a Keychain copy here is just a convenient secondary backup:

```bash
printf "MicroMart recovery code: " && read -rs PW && echo && security add-generic-password -a "$USER" -s "micromart-platform-recovery-code" -w "$PW" -U && unset PW
```

## If you got stuck at a `dquote>` prompt

Press **Ctrl+C** to cancel and get back to a normal prompt, then use the `read`-based commands
above instead of typing a password directly inside quotes.

## Checking what got stored (without printing the secret)

```bash
security find-generic-password -a "$USER" -s "micromart-platform" -w | wc -c
```

A non-zero length confirms something is stored, without displaying it. `-U` updates the entry
in place if it already exists, so it's safe to re-run these if a password changes.
