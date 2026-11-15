# Plan 1b (OAuth connect) — follow-ups and rulings

## Rulings
P1: T4 (browser e2e) is run by an implementer with browser tools (Playwright MCP); if no browser tool is available it returns BLOCKED and the controller runs the browser steps. — cost if wrong: one extra dispatch.
Ruling R1b (T2 Minor #4, plan defect): reuse oauth_client_id only if BOTH oauth_redirect_uri == redirect_uri AND the client was registered against the same base_url — new server field `oauth_base_url` — else re-register. — dev→prod base_url switch would reuse a client unknown to the new server — cost if wrong: one extra registration.
Ruling R2b (T2 Minor #3): the one-time code may appear in the /login redirect-to path (Guest case) — single-use + PKCE-bound, already in the original callback URL — accepted exception to "no code in URLs ERPNext builds".
Ruling R3b (T2 Minor #6): add a regression test asserting frappe.local.flags.commit is set after success and after _connect failure, reset in tearDown — the only production-only behaviour the suite can't otherwise see.
Ruling R4b (I1): on a site that is connected, an OAuth callback whose new key belongs to a different workspace (workspace.current) is REFUSED with "This site is connected to {A}. Disconnect first to switch to {B}." — nothing saved; and if _connect fails on a previously-connected site the previous key is restored. — switching workspaces is rare and must be deliberate — cost if wrong: one extra Disconnect click.

## Deferred minor findings
- Task 1: minor (deferred): scrub test doesn't exercise _scrub (error with spaces) — use {"error": "<code>"}; _error_code passes any [A-Za-z0-9_.-]{1,64} (allow-list RFC 6749 codes); redirect_allowed accepts userinfo/fragment; no tests for scheme-less URI / register 200 without client_id.
- Task 2: minor (deferred): orphaned key if _server_save fails after exchange (no revoke API); atomic-claim test is sequential; error text attacker-influenced → T3 renders as text.
- Task 3: minor (deferred): Disconnect alert host from window.location vs oauth_redirect_uri host; "undefined" if res.workspace missing.
- Task 4: minor (deferred): dev/install.sh should set host_name=http://localhost:8080 on frontend (nginx drops port → redirect_uri without port).
- Final: minor (deferred): paste-key Reconnect lacks the workspace-switch guard (move check to _connect); webhook changes not undone if _connect fails mid-way (Reconnect repairs); refused switch leaves an unused key in the other workspace; _restore_key failure masks original error.

## DialogBrain-side follow-ups
- Consent page Cancel should redirect to redirect_uri with error=access_denied&state=… (today it goes to the DialogBrain home).
- API-key self-revoke endpoint usable with the key itself (Disconnect could revoke; reconnects leave older 'ERPNext — <host> MCP' keys).
- Consider 'Lock the key to this workspace' on by default for OAuth clients.
