# OAuth Connect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An ERPNext admin links the site to a DialogBrain workspace either by "Connect with DialogBrain" (OAuth, no key copying) or by pasting an API key, both ending in the existing Connect flow.

**Architecture:** ERPNext acts as a dynamically-registered public OAuth client (authorization code + PKCE S256) of DialogBrain's existing MCP OAuth server. The token endpoint returns a regular `db_live_…` API key as `access_token`; it is stored in the existing `api_key` Password field and the existing `connect()` logic runs unchanged. The redirect goes through the admin's browser, so private (non-public) sites work too.

**Tech Stack:** Frappe/ERPNext v15+v16 app `dialogbrain`, `requests`, Frappe cache (Redis), Frappe form JS.

**Spec:** `docs/superpowers/specs/2026-11-13-oauth-connect-design.md` (short design). Parent spec: `docs/superpowers/specs/2026-11-13-dialogbrain-erpnext-inbox-design.md`.

## Global Constraints

- Branch `feat/oauth-connect` (based on `feat/plan-1-core-sync`); repo `/home/denys/projects/erpnext/dialogbrain_erpnext`; commit after every task; never `git stash`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python 3.10 syntax, no v16-only Frappe APIs. Tests subclass `dialogbrain.tests.utils.DialogBrainTestCase`. Run: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain --module dialogbrain.tests.test_oauth`.
- DialogBrain OAuth server facts (verified in `~/projects/dialogbrain/apps/api-gateway/src/api/mcp_oauth.py`):
  - Discovery: `GET {base_url}/.well-known/oauth-authorization-server` → `issuer`, `authorization_endpoint` (a FRONTEND URL, e.g. `http://localhost:3000/oauth/authorize` in dev, `https://dialogbrain.com/oauth/authorize` in prod), `token_endpoint`, `registration_endpoint`, `code_challenge_methods_supported: ["S256"]`, `authorization_response_iss_parameter_supported: true`.
  - Registration: `POST registration_endpoint` JSON `{"redirect_uris": [uri], "client_name": name}` → 201 `{"client_id", …}`, public client (`token_endpoint_auth_method: none`).
  - Redirect URI rules: `https` anywhere; `http` ONLY on `localhost` / `127.0.0.1` / `::1`; no fragment, no userinfo.
  - Authorize query: `response_type=code`, `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, `scope=mcp`. Callback receives `code`, `state`, and `iss` (or `error`).
  - Token: `POST token_endpoint` form-encoded `grant_type=authorization_code, code, redirect_uri, client_id, code_verifier` → `{"access_token": "db_live_…", "token_type": "bearer", "expires_in": 31536000, "refresh_token": <same key>}`.
  - No API-key self-revoke endpoint usable with the key itself (`DELETE /api/developer/keys/{id}` needs a user JWT) → Disconnect tells the admin where to revoke.
- Secrets: the API key, the authorization code and the PKCE verifier never go into a URL ERPNext builds, a log, an error message, `frappe.msgprint`, or the browser. Error texts from DialogBrain pass through `client.mask`/scrub.
- Callback path: `/api/method/dialogbrain.setup.oauth.callback`. Only a logged-in System Manager who started the flow can complete it; `state` is single-use (atomic claim: the request whose Redis `DELETE` of the state key returns 1 wins; any other refuses), 10-minute TTL, bound to `frappe.session.user`.
- Callback UX: EVERY outcome of the callback is a redirect, never a raw error page. The method is `allow_guest=True`: if `frappe.session.user == "Guest"` (session expired during consent) it does NOT touch the state and redirects to `/login?redirect-to=<urlencoded full callback URL with its query>`; after login the callback runs normally. Role is checked with `ROLE in frappe.get_roles()` (not `frappe.only_for`) and a failure is stored as a result like the other refusals.
- `iss` is MANDATORY on the callback: missing or different from the expected issuer → refused. Expected issuer = metadata `issuer` from discovery, unless site config `dialogbrain_oauth_issuer` is set (dev only: the browser-facing API host differs from the container-facing one — `http://localhost:8000` vs `http://host.docker.internal:8000` — and DialogBrain derives `issuer` from the request Host). `dev/install.sh` sets `bench --site frontend set-config dialogbrain_oauth_issuer http://localhost:8000`. Production never sets it.
- Known server behaviour: DialogBrain keeps a registered client in Redis for a limited time (about a day unused, ~90 days after a successful authorization). A stale `oauth_client_id` still works today (an empty redirect list is not checked) but the consent page then shows a neutral name. Accepted; `start` re-registers when the redirect URI changes.
- Key prefixes: treat any `db_…` key as a DialogBrain key. `client.mask` becomes prefix-agnostic (`db_…` + last 4) and errors are scrubbed by replacing the actual secret values (key, code, verifier), not by prefix matching.
- Live e2e only against the local dev DialogBrain (API `http://localhost:8000` from the browser, `http://host.docker.internal:8000` from containers; frontend `http://localhost:3000`), workspace **Test User (128)**. Never print `dev/dialogbrain-dev.env`. Never send messages to real threads.

## Review Focus

1. **Forged / replayed callback** (attacker sends the admin a link with their own `code` and a guessed or reused `state`) → refused, nothing saved; the site never binds to a foreign workspace. Test in Task 2.
2. **Callback opened by a different logged-in user** than the one who started → refused. Test in Task 2.
3. **Site on plain `http` with a non-loopback host** (e.g. `http://erp.local`) → OAuth start refuses with a clear message pointing to "Connect with API key"; no client registered. Test in Task 2.
4. **User denies consent** (`error=access_denied`) or the token exchange fails → Settings shows the reason, the existing connection (if any) is untouched. Test in Task 2.
5. **Site URL changes after a client was registered** (new domain) → a new client is registered for the new redirect URI instead of reusing a client whose redirect no longer matches. Test in Task 2.

---

### Task 1: OAuth protocol helpers

**Files:**
- Create: `dialogbrain/setup/oauth.py` (protocol helpers only in this task)
- Modify: `dialogbrain/client.py` (`mask` prefix-agnostic: `db_…` + last 4; update its tests)
- Modify: `dev/install.sh` (set `dialogbrain_oauth_issuer` on `frontend`)
- Test: `dialogbrain/tests/test_oauth.py`

**Interfaces:**
- Consumes: `dialogbrain.client.DialogBrainError(message, status=None, code=None)`, `dialogbrain.client.mask`.
- Produces (pure functions, no Frappe document access):
  - `discover(base_url: str) -> dict` — returns the metadata dict; raises `DialogBrainError` if unreachable, non-200, or missing any of `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, or if `S256` not in `code_challenge_methods_supported`.
  - `register_client(registration_endpoint: str, redirect_uri: str, client_name: str) -> str` — returns `client_id`.
  - `pkce_pair() -> tuple[str, str]` — `(verifier, challenge)`; verifier 43–128 chars from `secrets.token_urlsafe`; challenge = base64url(sha256(verifier)) without padding.
  - `authorize_url(authorization_endpoint: str, client_id: str, redirect_uri: str, state: str, challenge: str) -> str` — appends the query (keeps an existing query string).
  - `exchange_code(token_endpoint: str, code: str, verifier: str, client_id: str, redirect_uri: str) -> str` — returns the API key; raises `DialogBrainError` (message scrubbed of the code, verifier and any returned token) on non-200 or when `access_token` is missing or does not start with `db_`.
  - `redirect_allowed(uri: str) -> bool` — `https` any host, `http` only on `localhost`/`127.0.0.1`/`::1`.
  - Timeout 20 s on every request.

- [ ] **Step 1: Write failing tests** in `test_oauth.py` (`TestOAuthProtocol`), mocking `requests` (patch `dialogbrain.setup.oauth.requests`):
  - `test_discover_returns_metadata` / `test_discover_rejects_missing_s256` / `test_discover_unreachable_raises`.
  - `test_register_client_posts_redirect_and_name` — asserts JSON body `{"redirect_uris": ["https://erp.example.com/api/method/dialogbrain.setup.oauth.callback"], "client_name": "ERPNext — erp.example.com"}`, returns `client_id`.
  - `test_pkce_pair_matches_rfc7636` — challenge equals `base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()`; `43 <= len(v) <= 128`.
  - `test_authorize_url_has_all_params` — parse with `urllib.parse`; asserts `response_type=code`, `client_id`, `redirect_uri`, `state`, `code_challenge`, `code_challenge_method=S256`, `scope=mcp`.
  - `test_exchange_code_returns_key` — form body has `grant_type=authorization_code, code, redirect_uri, client_id, code_verifier`.
  - `test_exchange_code_error_hides_secrets` — 400 response; raised message contains neither the code, the verifier, nor any `db_live_` string.
  - `test_redirect_allowed` — `https://erp.example.com/x` True, `http://localhost:8080/x` True, `http://127.0.0.1/x` True, `http://erp.local/x` False, `ftp://x` False.
- [ ] **Step 2: Run, expect FAIL** (module missing).
- [ ] **Step 3: Implement** the functions above in `dialogbrain/setup/oauth.py`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat: OAuth protocol helpers for DialogBrain connect`.

### Task 2: OAuth start + callback, shared connect

**Files:**
- Modify: `dialogbrain/setup/oauth.py` (add whitelisted `start`, `callback`, `pop_result`)
- Modify: `dialogbrain/setup/connect.py` (extract `_connect(doc) -> dict` from `connect()`; add the new server fields to `SERVER_FIELDS`)
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.json` (fields `oauth_client_id` Data read_only, `oauth_redirect_uri` Data read_only hidden)
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.py` (`onload` exposes `oauth_result` via `pop_result`)
- Test: `dialogbrain/tests/test_oauth.py` (`TestOAuthFlow`)

**Interfaces:**
- Consumes: Task 1 helpers; `connect._client`, `connect._server_save`, `connect.SETTINGS`, `connect.ROLE`.
- Produces:
  - `CALLBACK_PATH = "/api/method/dialogbrain.setup.oauth.callback"`, `STATE_TTL = 600`, cache key `f"dialogbrain:oauth:{state}"`, result key `f"dialogbrain:oauth_result:{user}"`.
  - `connect._connect(doc) -> dict` — the body of today's `connect()` (same return dict); `connect()` becomes `frappe.only_for(ROLE); return _connect(frappe.get_single(SETTINGS))`.
  - `@frappe.whitelist(methods=["POST"]) start() -> dict` → `{"authorize_url": str}`. Steps: `only_for(ROLE)`; `redirect_uri = frappe.utils.get_url(CALLBACK_PATH)`; if not `redirect_allowed(redirect_uri)` → `frappe.throw(_("Sign-in with DialogBrain needs an https site address. Use \"Connect with API key\" instead."))`; `meta = discover(doc.base_url)`; reuse `doc.oauth_client_id` only if `doc.oauth_redirect_uri == redirect_uri`, else `register_client(meta["registration_endpoint"], redirect_uri, f"ERPNext — {host}")` and save both via `_server_save`; `verifier, challenge = pkce_pair()`; `state = secrets.token_urlsafe(32)`; cache `{"user", "verifier", "redirect_uri", "client_id", "token_endpoint", "issuer"}` for `STATE_TTL`; return `authorize_url(...)`.
  - `@frappe.whitelist(allow_guest=True, methods=["GET"]) callback(code=None, state=None, error=None, iss=None, **kwargs)` — always ends in a redirect (`frappe.local.response["type"] = "redirect"`, `["location"]`): Guest → `/login?redirect-to=…` without touching state; otherwise `/app/dialogbrain-settings`. Then: read the cache entry for `state` and claim it atomically (Redis `DELETE` returns 1). Refuse (result = error, nothing saved) when: no entry or claim lost; `entry["user"] != frappe.session.user`; `ROLE not in frappe.get_roles()`; `iss` missing or `!= entry["issuer"]` (expected issuer per Global Constraints, stored in the entry at `start`); `error` given (result text `_("DialogBrain sign-in was cancelled or refused: {0}").format(error)` with `error` truncated to 100 chars). Otherwise `key = exchange_code(...)`; set `doc.api_key = key`; if `doc.network_mode == "public"` and not `doc.public_site_url` → `doc.public_site_url = frappe.utils.get_url()` and the result carries `public_site_url_filled: <value>` so the form tells the admin what was filled in; `_server_save(doc)`; `res = _connect(doc)`; result = ok with `res`. Any `DialogBrainError`/`frappe.ValidationError` → result = error with its (masked) message; if `_connect` failed the stored key stays so the admin can press Connect again.
  - `pop_result(user) -> dict | None` — reads and deletes the result key; `{"ok": bool, "message": str, "workspace"?, "channels"?, "warning"?}`.
- [ ] **Step 1: Write failing tests** (`TestOAuthFlow`; patch `discover`, `register_client`, `exchange_code`, and `connect._connect` where noted; run as a System Manager test user via `frappe.set_user`):
  - `test_start_registers_client_and_caches_state` — returns an `authorize_url` with `state`; cache entry holds the session user and verifier; Settings gets `oauth_client_id` and `oauth_redirect_uri`.
  - `test_start_reuses_client_for_same_redirect` / `test_start_reregisters_when_site_url_changes` (Review Focus 5).
  - `test_start_refuses_plain_http_non_loopback` — patch `get_url` → `http://erp.local/...`; throws; `register_client` not called (Review Focus 3).
  - `test_start_requires_system_manager`.
  - `test_callback_happy_path_stores_key_and_connects` — `_connect` called once; `api_key` password equals the exchanged key; redirect location `/app/dialogbrain-settings`; `pop_result` ok.
  - `test_callback_unknown_state_refused` and `test_callback_state_is_single_use` — second call with same state refused; `exchange_code` called once total (Review Focus 1).
  - `test_callback_other_user_refused` (Review Focus 2).
  - `test_callback_iss_mismatch_refused`, `test_callback_missing_iss_refused`, `test_callback_issuer_override_from_site_config`.
  - `test_callback_guest_redirects_to_login_keeping_state` — as Guest: location starts with `/login?redirect-to=` and contains the callback path; the state entry still exists.
  - `test_callback_non_manager_redirects_with_error` — a logged-in user without System Manager who somehow holds a valid state gets a redirect and an error result, not an exception.
  - `test_state_claim_is_atomic` — two claims of the same state: exactly one wins.
  - `test_callback_access_denied_keeps_existing_key` — Settings already has a key; after `error=access_denied` the stored key is unchanged and `pop_result` has `ok: False` (Review Focus 4).
  - `test_callback_sets_public_site_url_when_empty`.
  - `test_server_fields_protected` — a form save (no server flag) cannot change `oauth_client_id` / `oauth_redirect_uri` (R14).
  - `test_no_secret_in_redirect_or_result` — location and stored result contain neither the key, the code, nor the verifier.
  - Existing `test_connect` must still pass unchanged (shared `_connect`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** as specified; migrate both sites (`bench --site frontend migrate`, `bench --site test.localhost migrate`).
- [ ] **Step 4: Run `test_oauth` and `test_connect`, expect PASS.**
- [ ] **Step 5: Commit** `feat: connect to DialogBrain via OAuth (start + callback)`.

### Task 3: Settings form — two ways to connect

**Files:**
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.js`
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.json` (section "Connect with API key" collapsible holding `base_url`, `api_key`)
- Modify: `README.md` (install + connect instructions: both ways; http-site limitation; behind a reverse proxy set `host_name` in site_config.json or forward `X-Forwarded-Proto`, otherwise the site looks like `http` and OAuth refuses; where to revoke the key)

**Interfaces:**
- Consumes: `dialogbrain.setup.oauth.start`, `frm.doc.__onload.oauth_result`.
- Produces: UI only.

- [ ] **Step 1: Implement**:
  - Not connected: primary button `__("Connect with DialogBrain")` → `frappe.call("dialogbrain.setup.oauth.start")` → `window.location.href = r.message.authorize_url`; secondary button `__("Connect with API key")` → existing `connect` call (requires `api_key`; if empty, expand the API-key section and focus it).
  - Connected: unchanged buttons (Reconnect/Re-sync/Check webhook/Disconnect) plus `__("Reconnect with DialogBrain")` in the group.
  - On load, if `__onload.oauth_result`: ok → green alert `Connected to {workspace}: {channels} channels, history import queued`, `show_warning(res.warning)`, and if `public_site_url_filled` a notice `Webhook address set to {0} — change it if DialogBrain can't reach this address`; error → `frappe.msgprint` with the message, red indicator.
  - After Disconnect: alert `__("Disconnected. You can revoke the key \"ERPNext — {0}\" in DialogBrain → Settings → Developer.")` with the site host.
- [ ] **Step 2: Verify** the form renders on `frontend` (`bench --site frontend clear-cache`, open `http://localhost:8080/app/dialogbrain-settings` in a browser, screenshot both states). Run the full suite: expect all OK.
- [ ] **Step 3: Commit** `feat: Settings form offers OAuth and API-key connect`.

### Task 4: Live e2e against local dev DialogBrain

**Files:** none (results in the commit body of an empty commit `test: OAuth connect e2e against dev DialogBrain`).

- [ ] **Step 1:** On `frontend` (logged in as Administrator), press Disconnect. Confirm webhook removed in DialogBrain (`GET /api/v1/webhooks` with the dev key via `sudo docker exec -e DB_KEY=...`, never printed).
- [ ] **Step 2:** Press "Connect with DialogBrain" → DialogBrain consent page at `localhost:3000` (browser already logged in to the dev account) → choose workspace **Test User**, keep "lock to workspace" on → Authorize → back in ERPNext Settings with the green alert.
- [ ] **Step 3:** Verify: `connected=1`, `workspace_id=128`, webhook active (Check webhook), channels count matches; a new API key named `ERPNext — localhost` (or as DialogBrain names it) exists in DialogBrain Settings → Developer.
- [ ] **Step 4:** Negative: start again and press Cancel/Deny on the consent page (if the page has none, simulate by opening the callback URL with `error=access_denied` and the real `state`) → Settings shows the refusal and the connection from Step 2 is intact.
- [ ] **Step 5:** Leave `frontend` connected in public mode. Record pass/fail per step in the empty commit body; commit.
