# Connect a channel from ERPNext — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An employee connects their own WhatsApp or Telegram number from ERPNext, and the new channel is assigned to them even if the browser closes right after the scan.

**Architecture:** ERPNext proxies DialogBrain's `channels.connect_session.*` tools through `DialogBrainClient.execute`. A persistent `DB Channel Claim` row records who owns the pending connection. One `claims.resolve` function binds the owner, and it is called from the browser poll, the every-minute tick and `upsert_channel`. A stored feature gate hides the button until DialogBrain ships the tools.

**Tech Stack:** Frappe/ERPNext v15 + v16 app `dialogbrain` (Python 3.10 syntax), Frappe dialog JS, `pyqrcode` (already a Frappe dependency).

**Spec:** `docs/superpowers/specs/2026-11-16-connect-channel-design.md`. Read all of it; "Resolving a claim" and "Anti-goals" bind every task.

**Branch:** `feat/connect-channel`, from `main`.

## Global Constraints

- Python 3.10 syntax, no v16-only Frappe API. CI runs v15 and v16.
- Every user-visible string: Python `_()`, JS `__()`. Source language is English. ru and uk translations are in Task 6.
- DialogBrain is never called in tests. Use a fake client with `execute(tool_id, params)` and `list_tools()`; errors are `DialogBrainError(message, error_type=…)`.
- Tool ids, exactly: `channels.connect_session.start`, `.get`, `.submit`, `.cancel`, `.options`.
- Statuses, exactly: `waiting_scan`, `scanned`, `need_code`, `need_password`, `need_pairing_code`, `waiting_confirm`, `connected`, `expired`, `error`. Waiting statuses are all except `connected`, `expired` and `error`.
- A phone, code or password is never stored, never logged, and never placed in a parameter or local variable whose name lacks `secret`.
- Writes to `DB Channel` and `DB Channel Claim` from these endpoints use `ignore_permissions=True` or `frappe.db`. Chat Agent has no write permission there.
- Run tests with `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain`. The whole suite stays green. New code is written test-first. Never touch `erpnext-db-1`; never run `docker compose up` without naming services.
- Commit after every task. Attribution line: `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Review Focus

1. **The tab closes right after the scan.** The channel must still end up with the owner in `allowed_users`, through the tick sweep. Test in Task 3.
2. **The first webhook arrives before any poll.** `upsert_channel` creating a channel while a claim of that type is open must never leave `allowed_users` empty. Tests in Task 3.
3. **An exception inside `submit`.** Nothing of the phone, code or password may reach Error Log. Test in Task 2.
4. **Another user guesses a claim name.** `get`, `submit` and `cancel` answer "not found", the same as for a missing claim. Test in Task 2.
5. **The dialog closes after `connected`.** No `cancel` is sent to DialogBrain. Test in Task 2.

---

### Task 1: Claim DocType and feature gate

**Files:**
- Create: `dialogbrain/dialogbrain/doctype/db_channel_claim/` (`db_channel_claim.json`, `.py`, `__init__.py`), `dialogbrain/connect_gate.py`
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.json` (field `connect_channel_options`: JSON, hidden, read-only), `dialogbrain/setup/connect.py` (add the field to the server-managed list beside `agent_tool_names`; call `connect_gate.refresh` after channels are upserted; clear it on disconnect), `dialogbrain/setup/agent_modes.py` (`hourly` calls `connect_gate.refresh` in its own try/except)
- Test: `dialogbrain/tests/test_connect_gate.py`

**Interfaces:**
- Produces: DocType `DB Channel Claim`.
  - Naming: hash.
  - Fields: `session_id` Data; `requested_by` Link User (reqd); `owner_user` Link User; `channel_type` Data (reqd); `method` Data; `status` Select `open\nconnected\nexpired\ncancelled\nfailed` (default open); `account_id` Data; `expires_at` Datetime; `resolved_at` Datetime.
  - Indexes on `status` and `session_id`.
  - Permissions: System Manager read and delete only.
- Produces: `connect_gate.refresh(client) -> dict` and `connect_gate.options() -> dict | None`.
  - `options()` returns `None` when the feature is off. Otherwise it returns `{"whatsapp": ["qr", …], "telegram": ["qr", …]}`.
  - `DEFAULT = {"whatsapp": ["qr"], "telegram": ["qr"]}`.

- [ ] **Step 1: Write failing tests** in `test_connect_gate.py`:
  - `test_off_without_start_tool`: the catalog lacks `channels.connect_session.start`. `refresh` stores nothing and `options()` is `None`.
  - `test_default_without_options_tool`: the catalog has start, get, submit and cancel only. `options() == DEFAULT`.
  - `test_options_tool_controls_methods`: the fake `options` tool returns `{"channels": {"whatsapp": ["qr", "pairing_code"], "viber": ["qr"]}}`. `options()` equals that `channels` dict, and unknown method names are dropped (allowed: qr, pairing_code, phone).
  - `test_refresh_failure_keeps_previous`: `list_tools` raises `DialogBrainError`. The stored value is unchanged.
  - `test_hourly_refreshes_gate`: `agent_modes.hourly()` with a patched client updates the field.
- [ ] **Step 2: Run them; expect import failures.**
- [ ] **Step 3: Implement** the DocType, the Settings field and `connect_gate.py`. `refresh` reads `client.list_tools()`. It calls `client.execute("channels.connect_session.options", {})` only if that id is in the catalog.
- [ ] **Step 4: Run `bench --site test.localhost migrate`, then the tests; expect PASS.**
- [ ] **Step 5: Commit.**

### Task 2: Session endpoints

**Files:**
- Create: `dialogbrain/api/connect_channel.py`, `dialogbrain/compat.py` addition `rate_limited(key: str, limit: int, seconds: int) -> bool`
- Test: `dialogbrain/tests/test_connect_channel.py`

**Interfaces:**
- Consumes: `connect_gate.options()`, `claims.resolve` (Task 3; until then call through `frappe.get_attr("dialogbrain.claims.resolve")`, patched in tests).
- Produces (whitelisted, `methods=["POST"]`):
  - `options() -> {"channels": dict | None, "can_choose_owner": bool}`
  - `start(channel: str, method: str = "qr", owner: str | None = None) -> state`
  - `get(claim: str) -> state`
  - `submit(claim: str, kind: str, secret: str) -> state`
  - `cancel(claim: str) -> {"ok": True}`
  - `state = {"claim", "status", "qr_svg", "pairing_code", "expires_at", "error", "channel", "message"}`
- `compat.rate_limited` counts in `frappe.cache` (incr plus expire) and returns True when the count is over the limit. `frappe.rate_limiter.rate_limit(user_based=…)` is not in every v15 release, so we don't use it.

- [ ] **Step 1: Write failing tests:**
  - `test_agent_owner_is_self`: a Chat Agent calls `start("whatsapp")` with `owner` set to another user. The claim's `owner_user` is the caller.
  - `test_manager_picks_owner_or_shared`: the owner must be an enabled user with Chat Agent, else `frappe.ValidationError`. `owner=""` gives a claim with no owner.
  - `test_plain_user_refused`: a user without the three inbox roles gets `frappe.PermissionError`.
  - `test_feature_off_refused` and `test_unlisted_channel_or_method_refused`.
  - `test_start_sends_external_ref`: the fake receives `{"channel": "whatsapp", "method": "qr", "external_ref": <claim name>}`. The state has `status == "waiting_scan"`, and `qr_svg` starts with `<svg`.
  - `test_qr_data_url_passed_through`: `qr` of `"data:image/png;base64,AAA"` comes back as `qr_svg == '<img src="data:image/png;base64,AAA">'`.
  - `test_foreign_claim_not_found`: `get`, `submit` and `cancel` by another user raise `frappe.DoesNotExistError` with the same message as a missing claim.
  - `test_submit_passes_kind`: `submit(claim, "code", "12345")` sends `{"session_id": …, "code": "12345"}`. A kind outside phone, code and password gives `ValidationError`.
  - `test_submit_secret_not_logged`: the fake raises `RuntimeError`. No `Error Log` row and no `frappe.local.form_dict` value contains `"12345"`.
  - `test_get_refreshes_expiry`: `expires_at` on the claim follows the fake's value.
  - `test_get_connected_resolves`: status `connected` with `account_id` calls `claims.resolve(claim_doc, state)` once.
  - `test_expired_marks_claim`.
  - `test_cancel_only_while_waiting`: when open and waiting, the fake receives cancel and the claim is cancelled. After connected, the fake receives nothing.
  - `test_start_rate_limit`: the 11th start within 10 minutes raises `frappe.TooManyRequestsError`.
  - `test_submit_rate_limit`: the 11th submit on one claim raises the same.
  - `test_errors_are_explained`: `error_type="billing_required"` and `"permission_denied"` give the two fixed messages below. Any other text has `db_live_…` / `db_test_…` keys scrubbed and is cut to 300 characters.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** Fixed messages:
  - billing: `_("Connecting channels is not included in this DialogBrain workspace's plan")`
  - permission: `_("The DialogBrain key needs edit rights in the workspace to connect channels")`
  - not found: `_("Connection not found")`
  
  In `submit`, delete `secret` from `frappe.local.form_dict` before calling DialogBrain. Catch every exception and re-throw a clean `frappe.throw` without the original traceback.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit.**

### Task 3: Claims — resolve, sweep, fail closed

**Files:**
- Create: `dialogbrain/claims.py`
- Modify: `dialogbrain/sync/upsert.py` (`upsert_channel`, new-channel branch only), `dialogbrain/sync/reconcile.py` (`tick` calls `claims.sweep()` in a try/except before the reconcile check)
- Test: `dialogbrain/tests/test_claims.py`

**Interfaces:**
- Produces:
  - `claims.resolve(claim, state: dict) -> dict`, returning `{"channel": name, "assigned": bool, "message": str | None}`
  - `claims.sweep(client=None) -> int`
  - `claims.on_new_channel(account_id: str, channel_type: str, client=None) -> list[str]`, returning the users to put in `allowed_users`
- `GRACE_SECONDS = 600`, `MAX_LOOKUPS = 3`, `LOOKUP_TIMEOUT = 5`.

- [ ] **Step 1: Write failing tests:**
  - `test_resolve_new_channel_assigns_owner`: the DB Channel is created with the `account_id` and `allowed_users == [owner]`. The claim is `connected` with `account_id` and `resolved_at` set.
  - `test_resolve_shared_leaves_allowed_empty`.
  - `test_resolve_existing_unowned_assigns`, and `test_resolve_existing_other_owner_untouched`: `assigned` is False, the message is `_("This number is already assigned to another employee")`, and the other user's name or email appears nowhere in the result.
  - `test_resolve_twice_is_idempotent`: one channel row and one allowed user.
  - `test_resolve_as_chat_agent`: it runs under `frappe.set_user(agent)` without `PermissionError`.
  - `test_sweep_resolves_closed_tab`: an open claim with no browser. The fake `get` returns connected, and `sweep()` binds the owner.
  - `test_sweep_expires_old_claims`: past `expires_at + GRACE_SECONDS`, the status is `expired` and no client call is made.
  - `test_sweep_skips_disconnected_site_and_survives_errors`.
  - `test_webhook_first_matches_claim`: `upsert_channel("acc-9", "whatsapp")` with an open whatsapp claim whose fake `get` returns `account_id == "acc-9"`. `allowed_users == [owner]`.
  - `test_webhook_first_external_ref`: `upsert_channel` is given `external_ref=<claim name>`. It binds without any client call.
  - `test_webhook_first_fails_closed`: two open whatsapp claims and none matches. The channel is created with both owners in `allowed_users`, never empty.
  - `test_no_open_claims_unchanged`: no client call, and `allowed_users` is empty as today.
  - `test_lookup_bounded`: with 5 open claims there are at most `MAX_LOOKUPS` client calls.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
  - `resolve` locks the claim row (`frappe.db.get_value(…, for_update=True)`) and calls `upsert_channel`.
  - It sets the child rows with `frappe.get_doc(…).save(ignore_permissions=True)`.
  - It then runs `agent_modes` for that channel in a try/except and calls `frappe.publish_realtime("dialogbrain_channel_connected", {"channel": name}, after_commit=True)`.
  - `upsert_channel` gains an optional `external_ref=None` parameter. It calls `claims.on_new_channel` only on its insert branch, and only when an open claim of that `channel_type` exists (one indexed `frappe.db.exists`).
- [ ] **Step 4: Run the whole suite; expect PASS.**
- [ ] **Step 5: Commit.**

### Task 4: Dialog

**Files:**
- Create: `dialogbrain/public/js/connect_channel.js`
- Modify: `dialogbrain/hooks.py` (`app_include_js`, or the existing desk bundle import), `dialogbrain/dialogbrain/page/dialogbrain_inbox/dialogbrain_inbox.js` (menu item), `dialogbrain/dialogbrain/doctype/db_channel/db_channel_list.js` (new; list button)

**Interfaces:**
- Consumes: the Task 2 endpoints.
- Produces: `dialogbrain.connect_channel.open()`.
  - It first calls `options`. When `channels` is null it shows `__("Your DialogBrain server does not support connecting channels from here yet")` and no dialog.

- [ ] **Step 1: Implement** the dialog per the spec's "Dialog" section.
  - Poll `get` every 3000 ms. Pause while `document.hidden`. After a failed poll, back off to 6 s and then 12 s.
  - Show `state.qr_svg` or `state.pairing_code`, with a countdown to `expires_at`.
  - Input rows: phone for `need_pairing_code` and the Telegram phone method; code for `need_code`; a password field for `need_password`. Each is sent as `submit(claim, kind, secret)` and cleared from the DOM after sending.
  - "Start again" on `expired`.
  - On `connected`, show `state.message` or `__("Connected")` and stop polling.
  - On hide: call `cancel` only if the last status was a waiting one.
- [ ] **Step 2: Build and check in the browser.** Run `bench build --app dialogbrain`, then `bench --site frontend clear-cache`. On the dev stand, patch `connect_gate.options` and the client with a fake from `bench console` (or a temporary server script), and walk QR → connected and Telegram phone → code → password → connected at 1440 px and 390 px. Record what was checked in the task report.
- [ ] **Step 3: Commit.**

### Task 5: Realtime refresh and channel form hint

**Files:**
- Modify: `dialogbrain/public/js/inbox/realtime.js` (on `dialogbrain_channel_connected`, reload channel types and the list), `dialogbrain/dialogbrain/doctype/db_channel/db_channel.js` (list intro: who the channel is assigned to; managers only)
- Test: `dialogbrain/tests/test_claims.py`. Add `test_realtime_published_on_resolve`, which patches `frappe.publish_realtime` and asserts the event name and payload.

- [ ] **Step 1: Write the failing test. Step 2: run it. Step 3: implement. Step 4: run. Step 5: commit.**

### Task 6: Translations, README, v15

**Files:**
- Modify: `dialogbrain/translations/ru.csv`, `uk.csv`, `README.md` (new section "Connecting a channel from ERPNext": who can, the methods, what "assigned" means, the DialogBrain version needed)
- Test: the existing `dialogbrain/tests/test_translations.py` must pass with the new strings.

- [ ] **Step 1: Add every new `_()` / `__()` string** to both CSV files, with placeholders intact.
- [ ] **Step 2: Run the translation tests and the whole suite on v16. Then run `bash dev/test-v15.sh`; expect PASS on both.**
- [ ] **Step 3: Commit and push. Open a PR against `main`. Do not merge; an outside review comes first.**
