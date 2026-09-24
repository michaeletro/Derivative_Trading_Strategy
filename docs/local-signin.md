# Automatic local browser sign-in (0.10.1)

## Run without copying the token

Reuse the profile already used to start this checkout. The updated launcher opens
an authenticated browser tab by default when `--profile` is explicitly selected:

```bash
python3 tools/start_dashboard.py --profile paper-tws --mode research
```

Omit `--mode research` for the profile's configured native mode. This does NOT
connect to IBKR or change Read-Only API, firewall rules, trading permissions, or
broker credentials. Profile settings/token still override stale shell exports.

To open another authenticated tab on an already running **updated** server:

```bash
python3 tools/open_dashboard.py --profile paper-tws
```

There is no rebuild or new server in the open-only command. An optional `--port`
is available when you deliberately launched the server on a different port.
The helper only contacts `http://127.0.0.1:<port>`, never arbitrary destinations,
never follows redirects, and ignores HTTP proxy environment variables. A profile
mismatch is an error, not grounds to disable authentication or overwrite a token.

For headless/manual use:

```bash
python3 tools/start_dashboard.py --profile paper-tws --no-open-browser
```

Legacy launches without --profile remain manual unless --open-browser is explicit.
The existing token form remains a memory-only fallback. No profile/token setup is
repeated. Missing or unsafe profile files must be resolved locally; this change
never fetches or regenerates a user's saved token automatically.

WSL uses Windows PowerShell interop to open the default Windows browser. The
short-lived handoff URL is supplied to PowerShell over stdin, not interpolated
into a shell script. Interop must be enabled; failures are reported without
printing the credential. Native Linux uses its configured graphical browser.
No actual Windows browser launch is claimed by the automated Linux tests.

## What is automatic, and what is not

The long-lived local credential stays in the existing private file:
`~/.config/derivative-lab/dashboard.token` (or the configured private directory).
It is NOT injected into HTML, JavaScript, a URL, a hidden form, an experiment,
a database record, browser localStorage/sessionStorage, or a committed config.
The auto-sign-in flow does not populate the token input at all.

On startup, the launcher generates a separate 256-bit, one-use handoff code and
public random startup nonce. Only the code/nonce/port are given to a small local
opener through an anonymous pipe. The helper does not inherit DTS_API_TOKEN or
other credential environment variables. The same code is seeded in the new
server. Its 60-second lifetime starts after database initialization. The helper
waits for the matching startup nonce, so it will not open a different listener.
It abandons the attempt if the parent exits or startup exceeds its deadline.

The open-only helper instead reads the saved profile and uses its Bearer token
to request one code from the running server's authenticated /api/auth/launch.
That endpoint does not accept browser cookies alone and never returns the saved
token. The long-lived token goes only in the loopback Authorization header.

The one-use code briefly occupies the URL fragment, not the query string. The
page removes the fragment with replaceState before posting the code for exchange.
The exchange requires same-origin JSON/custom-header access and atomically
consumes the code. It returns a fresh opaque session cookie with HttpOnly,
SameSite=Strict, host-only scope, and a port-specific name. It never returns the
session identifier in JSON. The long-lived profile token never reaches browser
JavaScript during this flow.

The opaque browser session is intentionally stored as a cookie, NOT in Web
Storage. It is a browser-session cookie with a server-side maximum of 12 hours
and in-memory server state. It works across refreshes and tabs in the same
browser. It expires at sign-out, server restart, or its deadline; browser session
restoration can retain session cookies, but cannot extend the server deadline.
Restarting via the profile launcher opens a fresh sign-in automatically. A new
browser, cleared cookies, or expiry can be handled with the open-only command.

Click **Sign out** to clear this tab's sensitive views immediately and revoke the
browser session at the server. Other tabs lose access on their next request.
Closing a tab does not revoke its shared session or stop recording. Sign out
does not disconnect IBKR, stop the application, or roll back an already committed
calculation. If sign-out transport fails, the page reports that revocation is
unconfirmed; retry Sign out or restart the server. Auth failures do not silently
reacquire a session. Manual token entry does not fall back to cookie auth if the
explicit token is wrong.

## Security boundary and limitations

This is a single-user **HTTP loopback** application, not an internet-facing login
service. It still binds only 127.0.0.1. Because it is HTTP, its cookie does not
claim the Secure attribute or use a __Host- prefix requiring HTTPS. Do not expose
this port through a tunnel, LAN bind, port forward, or shared reverse proxy. A
multi-user/remote deployment needs separately designed HTTPS authentication.

Cookies do not isolate TCP ports. A port-specific NAME avoids normal instance
collisions but is NOT a security barrier against a malicious service on another
port of the same host. The design trusts the user's machine, browser profile,
and other localhost services; use --no-open-browser and manual Bearer mode when
that boundary is inappropriate. SameSite alone is also not a CSRF defense across
localhost ports: cookie-authenticated API calls require X-DTS-Local-Request and
exact Origin for mutations, or exact Origin/same-origin Fetch Metadata for GETs.
Foreign Host/Origin/Fetch Metadata requests are rejected, with no permissive CORS.
The existing restrictive CSP and no-store/no-referrer headers remain.

Handoff codes are temporary credentials. They can briefly appear in browser/OS
history or local process inspection before consumption. Do not share an unused
link. Removal from the address bar is not a guarantee against extensions or a
compromised machine. HttpOnly restricts script access to the cookie, but does not
make an XSS-capable page unable to perform authenticated actions. Private disk
files are not encrypted and cannot protect a compromised OS user account.

Secrets are not logged by the helper/server, and unauthorized static/auth-status
responses contain no credentials. Auth tickets/sessions are bounded (8 pending
codes and 32 sessions) and pruned by expiry. A restart invalidates all sessions.
No auth data enters database backups, and no database migration is introduced:
the existing schema remains 5. Older schema migrations, when applicable, retain
their existing verified-backup behavior.

## Keep credentials out of Git

Private profiles remain outside the repository. Configuration creation/loading
now refuses the application source directory and any directory inside a detected
Git worktree (including .git files used by linked worktrees). The existing owner,
permissions, bounded-read, no-symlink/no-hardlink checks remain.
`.gitignore` also excludes dashboard.token and credential-directory patterns as
defense in depth. Git's force-add and manual copies can defeat ignore rules; do
not copy real credentials into source, issues, screenshots, or commits. Development
and CI use only isolated generated/fixture credentials, never the owner's files.

## Update and verify

Preserve edits and stop the previous server with Ctrl+C; wait for Shutdown complete.
Update the existing review checkout, then use its saved profile:

```bash
git fetch origin feature/local-browser-signin
git switch --detach FETCH_HEAD
python3 tools/start_dashboard.py --profile paper-tws --mode research
```

Check that the browser opens with HTTP service Available and an empty token field;
refresh and open a second tab; sign out and check that neither tab retains API
access. Reopen with tools/open_dashboard.py --profile paper-tws. Broker Connect
remains a separate explicit action. Stop cleanly as before to finish the backup.

Tests cover atomic one-use consumption, ticket/session expiry, host binding,
bounded state, cookie flags, CSRF/Origin/Fetch Metadata, explicit bad Bearer
precedence, logout/restart, no credential in static responses or access logs,
no redirect/proxy forwarding, sanitized helper failures, Windows stdin handoff,
profile permissions/repository exclusion, and actual-browser refresh/new-tab
sign-in. Browser tests use the actual C++ server and temporary credentials.

References:
- https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Sec-Fetch-Site
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies
