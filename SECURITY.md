# Security Policy

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/Pasblinn/yoosee-nvr/security/advisories/new).
Please do not open a public issue for a vulnerability.

Include what you can: affected version or commit, reproduction steps, and impact.
Expect a first response within 7 days.

## What this software handles

Running `yoosee-nvr` means one machine holds, at the same time:

- **Camera credentials** — RTSP username and password, in `.env`
- **Cloud credentials** — an OAuth refresh token in `~/.config/rclone/rclone.conf`,
  usually with full access to a Google Drive account
- **Video and audio of the inside of a home**
- **A web dashboard** that serves all of the above

Any one of these leaking is serious. The defaults are chosen accordingly.

## Defaults, and what they assume

| Default | Why |
|---|---|
| `WEB_HOST=127.0.0.1` | The dashboard is not exposed to the network unless you say so |
| No default password | Startup fails rather than run with a password published in this repo |
| `.env`, `certs/`, `rclone.conf`, footage in `.gitignore` | Secrets and video must never reach a commit |
| `.flask_secret_key` written `600` | Session keys persist across restarts without ending up in a config file |
| Sessions `HttpOnly`, `SameSite=Lax`, expiring | Limits cookie theft and cross-site abuse |
| `Secure` cookie only when TLS is on | A `Secure` cookie over plain HTTP would silently break login |
| Login rate limited, then locked out | Slows credential guessing on a LAN |
| Constant-time password comparison | No timing oracle on the password |
| `DASHBOARD_PASS_HASH` (PBKDF2-SHA256, 260k iterations) | A leaked `.env` does not immediately hand over the dashboard |
| Passwords redacted in logs | `config.rtsp_url(redacted=True)` is what reaches the log file |

## Known limitations

Be aware of these before exposing anything:

- **The dashboard is single-user.** One username, one password, no roles, no audit
  trail of who watched what.
- **TLS is self-signed.** `scripts/harden.sh` generates a certificate that browsers
  will warn about. It encrypts the connection; it does not authenticate the server.
- **The login form has no CSRF token.** `SameSite=Lax` blocks the realistic
  cross-site POST, but a token would be better.
- **Rate limiting is per source IP and in memory.** It resets on restart, and a
  reverse proxy will collapse every client into one IP.
- **RTSP digest auth uses MD5.** That is what the protocol specifies and what these
  cameras support; on a LAN it is the camera's limitation, not a choice.
- **Camera passwords are stored recoverably.** ffmpeg needs the password to open the
  stream, so it cannot be hashed. Protect `.env` (`chmod 600`) and use a password
  that is not reused anywhere else.
- **`scripts/harden.sh` changes the host firewall and SSH.** Run `--dry-run` first.
  A wrong subnet plus a remote SSH session equals a lockout.

## If you exposed something

1. **Camera password** — change it in the Yoosee app, then update `.env`.
2. **Cloud token** — revoke the app in your Google account's security settings, then
   `rclone config` again.
3. **Dashboard password** — change it, and prefer `DASHBOARD_PASS_HASH`.
4. **A secret reached a commit** — rotating the credential is what actually fixes it.
   Rewriting history helps, but anything pushed to a public host should be treated as
   compromised regardless.
