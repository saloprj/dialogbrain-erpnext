# Connect ERPNext to DialogBrain via OAuth — short design

Status: approved 2026-11-13. Extends Plan 1 (Core & Sync); lands before Plan 2 (Inbox UI).

## Intent
Any ERPNext owner installs the `dialogbrain` app and links their site to their DialogBrain account themselves. Two ways, as with DialogBrain ↔ ChatGPT/Claude:
1. **"Connect with DialogBrain"** (OAuth) — log in to DialogBrain, pick a workspace, Authorize; no key copying.
2. **"Paste key"** — the existing flow (API key `db_live_…` → Connect).

## Goals / non-goals / anti-goals
- Goal: a non-technical admin connects in under a minute, public or private (no public URL) site.
- Goal: both ways end in the same existing `connect()` (capability check, workspace, webhook, channels, backfill).
- Non-goal: per-employee DialogBrain accounts. One ERPNext site ↔ one DialogBrain workspace; managers never open DialogBrain (earlier decision). Connecting is System Manager only.
- Non-goal: changes to DialogBrain's OAuth server, unless planning finds a hard gap.
- Anti-goal: the key in a URL, a log, the browser, or reachable by a non-admin; a callback that can bind a site to an attacker's workspace (CSRF / code injection).

## Approaches
- **A (chosen): ERPNext as a dynamically-registered OAuth client** of DialogBrain's existing MCP OAuth server (`apps/api-gateway/src/api/mcp_oauth.py`): discovery `/.well-known/oauth-authorization-server`, `POST /oauth/register` (DCR), `/oauth/authorize` (frontend consent page: login, workspace choice, "lock to workspace"), `POST /oauth/token` (authorization_code + PKCE S256). The token response's `access_token` IS a regular API key (`db_live_…`, expires_in 1 year, refresh_token = same key). No DialogBrain changes.
- B (rejected): one pre-registered "ERPNext" client in DialogBrain — needs DialogBrain changes, gains nothing since DCR exists.

## Design
- Settings form: buttons "Connect with DialogBrain" (primary) and "Paste API key" (secondary, current fields).
- OAuth start (whitelisted, System Manager, POST): discover endpoints from `base_url`; register a client once per site (redirect_uri = `<site>/api/method/dialogbrain.api.oauth.callback`, client_name `ERPNext — <site host>`), store client_id; create PKCE verifier + random `state`, keep them server-side (cache, ~10 min TTL) bound to the session user; return the authorize URL; the browser navigates to it.
- Callback (GET, logged-in System Manager who started it): validate `state` (single use, same user), exchange code + verifier at the token endpoint, store the key in the `api_key` Password field, run `connect()`, redirect to the Settings form with a success/error message. Never echo the key or code.
- Private sites work: redirect goes through the admin's browser, not the internet.
- Disconnect: as now (remove webhook); additionally revoke the key in DialogBrain if an API-key revoke endpoint is usable with the key itself — else document "revoke in DialogBrain → Settings → Developer".
- No DialogBrain account: the consent page sends the user to login/signup; planning verifies they come back to the authorize step.

## Left to the builder
Exact cache keys/TTL, error copy, how base_url maps to the frontend authorize URL (use discovery metadata), test doubles for the OAuth server; live e2e against local dev DialogBrain (localhost:3000 / :8000, workspace Test User 128).
