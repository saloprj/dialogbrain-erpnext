# DialogBrain for ERPNext — Plan 1: Core & Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An installable Frappe app `dialogbrain` that connects to a DialogBrain workspace with an API key and keeps a complete, permission-checked mirror of all conversations and messages in ERPNext (webhooks + backfill + cursor reconcile, or polling for private sites), can send replies, and links conversations to Customers/Leads.

**Architecture:** Thin HTTP client to DialogBrain (`/api/tools/{tool}/execute`, `/api/v1/webhooks`). Signed webhooks land in a guest endpoint that dedupes and enqueues; a single event processor upserts DocTypes and publishes realtime events. A scheduler tick runs cursor-based reconcile (5 min with webhooks, 1 min in polling mode). No UI in this plan beyond the Settings form (inbox UI = Plan 2).

**Tech Stack:** Frappe/ERPNext v15+v16 app (Python ≥3.10 syntax), `requests`, Frappe test runner (`FrappeTestCase`), frappe_docker dev stack in `/home/denys/projects/erpnext` (project `erpnext`, `pwd.yml`).

**Spec:** `docs/superpowers/specs/2026-11-13-dialogbrain-erpnext-inbox-design.md`

**Plan series:** 1 Core & Sync (this) → 2 Inbox UI → 3 Agent tools + DialogBrain signed thread context (+ `request_id` passthrough in `/api/tools/{id}/execute`) → 4 Agent modes, reports, translations, CI.

## Global Constraints

- App name `dialogbrain`, module `DialogBrain`, repo root `/home/denys/projects/erpnext/dialogbrain_erpnext` (its own git repo; commit after every task).
- Must run on Frappe/ERPNext v15 and v16 → Python 3.10-compatible syntax, no v16-only APIs (CI matrix comes in Plan 4; locally we run v16).
- DialogBrain auth header: `Authorization: Bearer db_live_…`. Tool calls: `POST {base_url}/api/tools/{tool_id}/execute` body `{"tool_id", "params", "mode": "auto", "skip_approval": true, "thread_id"?}` → `{"success", "data", "error", …}`.
- Webhook signature: header `X-DialogBrain-Signature: sha256=<hex>`, HMAC-SHA256 over the raw body, key = `bytes.fromhex(secret.removeprefix("whsec_"))`.
- Webhook events subscribed: `message.received`, `message.updated`, `message.deleted`, `conversation.started`, `conversation.updated`, `draft.created`, `handoff.requested`, `contact.updated`. `message.received` fires for outgoing too → direction from `data.message.is_outgoing`.
- Secrets (api_key, webhook_secret) only in `Password` fields; never logged (log masked `db_live_…abcd`).
- Webhook body limit 1 MB → 413.
- Reconcile: every 5 min in `webhook` mode, every 1 min in `polling` mode; cursor `last_reconcile_at` advances only after a fully successful pass; query window starts 2 min before the cursor.
- Backfill default 30 days (`backfill_days`).
- Messages ordered by DialogBrain `sent_at`, never by arrival.
- Customer-scoped data is never exposed via guest endpoints.
- Dev site for e2e: `frontend` (http://localhost:8080, reachable from DialogBrain-dev api-gateway as `http://host.docker.internal:8080`). Test site: `test.localhost`.
- Never `git stash`; WIP is committed on the current branch.

## Review Focus

1. **Webhook for an unknown thread or unknown channel account** (new channel connected after install, or webhook before backfill) → conversation and DB Channel are auto-created, nothing dropped. Test in Task 7.
2. **Same message delivered twice concurrently** (webhook retry + reconcile) → exactly one DB Message; a duplicate-key race is swallowed, not a 500. Test in Task 7.
3. **Echo of a reply sent from ERPNext arrives before the send job stored `message_id`** → matched to the pending DB Message (same conversation, outgoing, same text, within 120 s), not duplicated. Test in Task 8.
4. **Manager double-clicks Send / browser retries** → one DB Message and one DialogBrain call per `client_ref`. Test in Task 8.
5. **Phone formats differ** between DialogBrain (`380671234508`, `+380 67 123 45 08`) and ERPNext Contact (`067-123-45-08`) → still matched. Test in Task 5.

---

### Task 1: App scaffold, dev mount, test site

**Files:**
- Create: `pyproject.toml`, `README.md`, `license.txt` (MIT), `.gitignore`
- Create: `dialogbrain/__init__.py` (`__version__ = "0.1.0"`), `dialogbrain/hooks.py`, `dialogbrain/modules.txt` (`DialogBrain`), `dialogbrain/patches.txt`, `dialogbrain/dialogbrain/__init__.py`, `dialogbrain/config/__init__.py`
- Create: `dev/compose.dialogbrain.yml`, `dev/install.sh`
- Test: `dialogbrain/tests/test_smoke.py`

**Interfaces:**
- Produces: importable package `dialogbrain`; `dev/install.sh` = idempotent "make the app live in the running stack"; test command used by every later task:
  `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain [--module dialogbrain.tests.<mod>]`

- [ ] **Step 1:** Generate scaffold with `bench new-app` semantics (pyproject with `flit_core` build backend like ERPNext's; `hooks.py` with `app_name="dialogbrain"`, `app_title="DialogBrain"`, `app_publisher="DialogBrain"`, `required_apps=["erpnext"]`).
- [ ] **Step 2:** `dev/compose.dialogbrain.yml` — override for services `backend, queue-short, queue-long, scheduler, websocket` adding volume `../dialogbrain_erpnext:/home/frappe/frappe-bench/apps/dialogbrain` (path relative to `/home/denys/projects/erpnext`).
- [ ] **Step 3:** `dev/install.sh` — run from `/home/denys/projects/erpnext`:
  1. `sudo docker compose -p erpnext -f pwd.yml -f dialogbrain_erpnext/dev/compose.dialogbrain.yml up -d backend queue-short queue-long scheduler websocket` (named services only — never a bare `up`)
  2. in each of those containers: `env/bin/pip install -e apps/dialogbrain -q`
  3. append `dialogbrain` to `sites/apps.txt` if missing (shared volume)
  4. create `test.localhost` if missing (`bench new-site test.localhost --db-root-password admin --admin-password admin --install-app erpnext --mariadb-user-host-login-scope=%`), `bench --site test.localhost set-config allow_tests true`
  5. `bench --site test.localhost install-app dialogbrain` and same for `frontend` (skip if installed), then `bench --site <site> migrate`.
- [ ] **Step 4: Write failing test** `test_smoke.py::TestSmoke.test_app_installed`: `assert "dialogbrain" in frappe.get_installed_apps()`.
- [ ] **Step 5:** Run `bash dialogbrain_erpnext/dev/install.sh` then the test command with `--module dialogbrain.tests.test_smoke` → PASS. (Before step 3 is run it fails with "App dialogbrain not installed".)
- [ ] **Step 6: Commit** `feat: scaffold dialogbrain app and dev stack mount`.

### Task 2: DialogBrain HTTP client + recorded fixtures

**Files:**
- Create: `dialogbrain/client.py`
- Create: `dialogbrain/tests/fixtures/*.json` (recorded from dialogbrain-dev)
- Test: `dialogbrain/tests/test_client.py`

**Interfaces:**
- Produces:
  - `class DialogBrainError(Exception)` with `.status: int | None`, `.code: str | None`
  - `class DialogBrainClient(base_url: str, api_key: str, timeout: float = 20)`
    - `@classmethod from_settings() -> DialogBrainClient` (reads `DialogBrain Settings`, `get_password("api_key")`)
    - `execute(tool_id: str, params: dict, thread_id: str | None = None) -> dict | list` — returns `data`; raises `DialogBrainError` when HTTP ≥400 or `success` is false
    - `create_webhook(url: str, event_types: list[str]) -> dict` (`{"id", "secret"}`)
    - `delete_webhook(webhook_id: str) -> None`, `activate_webhook(webhook_id: str) -> None`
    - `list_webhooks() -> list[dict]`
  - `def mask(secret: str) -> str` → `"db_live_…" + last 4`
  - `def load_fixture(name: str) -> dict` in `dialogbrain/tests/utils.py`

- [ ] **Step 1: Record fixtures** from dialogbrain-dev (workspace 128) with a dev API key the user provides (or one created via Settings → Developer): responses of `workspace.current`, `channels.list`, `search.threads` (`since`, `limit=5`), `messages.read_history` (one thread, `limit=5`), `messages.send` (to a test livechat thread created by `agents_simulate_inbound create_new_livechat=true`), and one real webhook delivery body for `message.received` (capture with a temporary `webhook.site`-style local listener or by logging in Task 4). Strip personal data. Save as `tests/fixtures/<tool>.json`. Write down in `tests/fixtures/README.md` the exact field paths later tasks use (thread id, channel/account id, contact phone/email/name, message id, sent_at, is_outgoing, attachments).
- [ ] **Step 2: Failing tests** (patch `requests.Session.request`):
  - `test_execute_posts_tool_body`: called URL `…/api/tools/search.threads/execute`, JSON `{"tool_id": "search.threads", "params": {"limit": 5}, "mode": "auto", "skip_approval": True}`, header `Authorization: Bearer db_live_x`.
  - `test_execute_returns_data` → returns fixture `data`.
  - `test_execute_raises_on_success_false` → `DialogBrainError` with `.code` from `error`.
  - `test_execute_raises_on_http_401` → `.status == 401`.
  - `test_mask` → `mask("db_live_abcdef1234") == "db_live_…1234"`.
- [ ] **Step 3:** Run → FAIL (module missing).
- [ ] **Step 4:** Implement `client.py` (one `requests.Session`, JSON, timeout; error message never includes the key).
- [ ] **Step 5:** Run → PASS. **Step 6: Commit** `feat: DialogBrain HTTP client`.

### Task 3: DocTypes and roles

**Files:**
- Create doctype folders under `dialogbrain/dialogbrain/doctype/`: `dialogbrain_settings` (Single), `db_channel`, `db_channel_user` (child), `db_conversation`, `db_message`, `db_message_attachment` (child), `db_draft`, `db_webhook_log`
- Create: `dialogbrain/install.py` (`after_install`: create roles), hook `after_install = "dialogbrain.install.after_install"`
- Create: `dialogbrain/patches/v0_1/add_indexes.py` (or `on_doctype_update` in each controller — preferred)
- Test: `dialogbrain/tests/test_doctypes.py`

**Interfaces (field names are contracts for all later tasks):**
- `DialogBrain Settings`: `base_url` (Data, default `https://api.dialogbrain.com`), `api_key` (Password), `workspace_id` (Data, RO), `workspace_name` (Data, RO), `network_mode` (Select `public\|private`), `public_site_url` (Data), `webhook_id` (Data, RO), `webhook_secret` (Password), `webhook_status` (Data, RO), `last_reconcile_at` (Datetime, RO), `backfill_days` (Int, default 30), `backfill_status` (Data, RO), `default_company` (Link Company), `connected` (Check, RO)
- `DB Channel` (autoname `field:account_id`): `account_id`, `channel_type`, `title`, `company` (Link Company), `default_manager` (Link User), `agent_mode` (Select `copilot\|autopilot\|off`, default `copilot`), `enabled` (Check 1), `allowed_users` (Table `DB Channel User`: `user` Link User)
- `DB Conversation` (autoname `hash`): `thread_id` (Data, unique), `channel` (Link DB Channel), `title`, `contact_name`, `phone`, `email`, `handle`, `avatar_url`, `db_contact_id`, `party_type` (Link DocType, options limited to Customer/Lead), `party` (Dynamic Link), `contact` (Link Contact), `status` (Select `Open\|Closed`, default Open), `assigned_to` (Link User), `agent_override` (Select `default\|human\|agent`), `needs_human` (Check), `last_message_at` (Datetime), `last_message_preview` (Small Text), `last_inbound_at` (Datetime), `last_message_direction` (Select `in\|out`), `first_response_seconds` (Int), `unread_count` (Int), `opened_at`, `closed_at` (Datetime)
- `DB Message` (autoname `hash`): `message_id` (Data, unique, may be empty while pending), `conversation` (Link), `direction` (`in\|out`), `author_type` (`customer\|manager\|agent\|system`), `sent_by` (Link User), `text` (Long Text), `sent_at` (Datetime), `attachments` (Table `DB Message Attachment`: `file_id`, `filename`, `mime_type`, `media_type`, `size`, `download_url`), `delivery_status` (`pending\|sent\|failed\|unknown\|deleted`), `client_ref` (Data, unique), `error` (Small Text), `raw` (JSON)
- `DB Draft`: `draft_id` (unique), `conversation`, `text`, `created_at`, `status` (`pending\|approved\|rejected\|superseded`)
- `DB Webhook Log`: `delivery_id` (unique), `event`, `received_at`, `status` (`queued\|processed\|failed`), `error`
- Indexes: `DB Conversation(status, last_message_at)`, `(assigned_to, status)`, `(phone)`, `(email)`; `DB Message(conversation, sent_at)`.
- Roles: `Chat Agent`, `Chat Manager`. DocType perms: System Manager full; Chat Manager read/write all except Settings & Webhook Log; Chat Agent read/write DB Conversation, read DB Message/DB Draft/DB Channel (row filtering = Task 6).

- [ ] **Step 1: Failing tests:** `test_thread_id_unique` (second insert with same `thread_id` raises `frappe.UniqueValidationError` or `DuplicateEntryError`), `test_message_id_unique`, `test_client_ref_unique`, `test_roles_exist`, `test_conversation_indexes` (query `SHOW INDEX FROM tabDB Conversation` contains a key with columns `status,last_message_at`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Create DocType JSONs + controllers (`on_doctype_update` adds indexes via `frappe.db.add_index`). **Step 4:** `bench --site test.localhost migrate` (and `frontend`), run → PASS. **Step 5: Commit** `feat: dialogbrain doctypes and roles`.

### Task 4: Webhook receiver

**Files:**
- Create: `dialogbrain/sync/__init__.py`, `dialogbrain/sync/signature.py`, `dialogbrain/api/__init__.py`, `dialogbrain/api/webhook.py`
- Test: `dialogbrain/tests/test_webhook.py`

**Interfaces:**
- Consumes: Settings fields (Task 3).
- Produces:
  - `verify_signature(raw_body: bytes, header: str | None, secret: str) -> bool` (constant-time compare)
  - `@frappe.whitelist(allow_guest=True, methods=["POST"]) receive()` at `/api/method/dialogbrain.api.webhook.receive` → `{"ok": true}`; enqueues `dialogbrain.sync.processor.process_delivery(log_name: str)` on queue `short`, `enqueue_after_commit=True`.

- [ ] **Step 1: Failing tests:**
  - `test_valid_signature` / `test_invalid_signature` / `test_missing_header` with secret `"whsec_" + "ab"*32` and HMAC computed in the test.
  - `test_receive_rejects_bad_signature` → HTTP 401 (`frappe.local.response.http_status_code`), no `DB Webhook Log` row.
  - `test_receive_rejects_large_body` (1 MB + 1) → 413.
  - `test_receive_dedupes_delivery_id`: same signed body twice → one log row, `frappe.enqueue` (patched) called once, both return ok.
  - `test_receive_when_not_connected` → 503, nothing stored.
- [ ] **Step 2:** FAIL. **Step 3:** Implement (read `frappe.request.get_data()`; store `raw` body in the log only as JSON payload; insert log with `ignore_permissions`; duplicate `delivery_id` → return ok). **Step 4:** PASS. **Step 5: Commit** `feat: signed webhook receiver`.

### Task 5: Party linking

**Files:**
- Create: `dialogbrain/party.py`
- Test: `dialogbrain/tests/test_party.py`

**Interfaces:**
- Produces:
  - `normalize_phone(raw: str | None) -> str | None` — digits only, strip leading `00`; `None` if < 7 digits
  - `phones_match(a: str, b: str) -> bool` — equal normalized, or both ≥ 9 digits and last 9 digits equal
  - `find_parties(phone: str | None, email: str | None) -> list[tuple[str, str]]` — `(party_type, party)` via `Contact Phone`/`Contact Email` → `Dynamic Link` (Customer before Lead; also `Lead.mobile_no/phone/email_id`)
  - `link_party(conversation: str) -> tuple[str, str] | None` — link only when exactly one Customer (else exactly one Lead) matches; sets `party_type`, `party`, `contact`; never overrides a manually set party
  - `@frappe.whitelist() set_party(conversation: str, party_type: str, party: str) -> None` — manual link; adds conversation phone/email to the party's primary Contact if missing (creates Contact if none)
  - `@frappe.whitelist() create_lead(conversation: str) -> str`

- [ ] **Step 1: Failing tests:** `test_normalize_phone` table (`"+380 67 123 45 08"→"380671234508"`, `"00380671234508"→"380671234508"`, `"123"→None`); `test_match_local_vs_international` (`"067-123-45-08"` vs `"380671234508"` → True — Review Focus 5); `test_autolink_single_customer`; `test_no_autolink_when_two_customers`; `test_lead_when_no_customer`; `test_manual_link_not_overridden`; `test_set_party_adds_phone_to_contact`; `test_create_lead_links_conversation`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement (SQL on `tabContact Phone` comparing last 9 digits via `RIGHT(REGEXP_REPLACE(phone,'[^0-9]',''),9)` — MariaDB ≥10.0.5 supports it — then confirm in Python with `phones_match`). **Step 4:** PASS. **Step 5: Commit** `feat: link conversations to customers and leads`.

### Task 6: Permissions

**Files:**
- Create: `dialogbrain/permissions.py`; Modify: `dialogbrain/hooks.py` (`permission_query_conditions`, `has_permission` for `DB Conversation`, `DB Message`, `DB Draft`)
- Test: `dialogbrain/tests/test_permissions.py`

**Interfaces:**
- Produces:
  - `allowed_channels(user: str) -> list[str] | None` — `None` = all (System Manager, Chat Manager); Chat Agent → channels whose `allowed_users` is empty or contains user
  - `permitted_users(channel: str) -> list[str]` — enabled users with Chat Manager, plus Chat Agents allowed on channel
  - `conversation_query(user: str) -> str`, `conversation_has_permission(doc, ptype, user) -> bool`; message/draft variants delegate to their conversation
  - `assert_can_access(conversation: str, user: str | None = None) -> None` (raises `frappe.PermissionError`) — used by every whitelisted inbox method

- [ ] **Step 1: Failing tests:** agent sees conversation of an open channel; agent does not see a channel restricted to another user (`frappe.get_list` and `frappe.has_permission`); manager sees all; messages follow conversation; `permitted_users` contents; user without chat roles sees nothing.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: channel-based permissions`.

### Task 7: Event processor

**Files:**
- Create: `dialogbrain/sync/processor.py`, `dialogbrain/sync/upsert.py`, `dialogbrain/sync/realtime.py`
- Test: `dialogbrain/tests/test_processor.py` (uses fixtures from Task 2)

**Interfaces:**
- Consumes: DocTypes (Task 3); `link_party(conversation_name)` (Task 5); `permitted_users(channel: str) -> list[str]` (Task 6).
- Produces:
  - `process_delivery(log_name: str) -> None` — routes by event, sets log status
  - `process_event(event: str, data: dict) -> None`
  - `upsert_channel(account_id: str, channel_type: str, title: str | None = None) -> str`
  - `upsert_conversation(thread: dict, channel: str) -> str` (returns name; calls `link_party` on create and on `contact.updated`)
  - `upsert_message(conversation: str, message: dict) -> tuple[str, bool]` (name, created)
  - `apply_message_counters(conversation: str, message_name: str) -> None` — preview, `last_message_at` (max, never moves backwards), `last_inbound_at`, `last_message_direction`, `unread_count += 1` for inbound, reopen Closed on inbound (`status=Open`, `opened_at=now`, `first_response_seconds=None`), set `first_response_seconds` on first outgoing after `opened_at`
  - `publish(conversation: str, kind: Literal["conversation","message"], payload: dict)` → `frappe.publish_realtime("dialogbrain:"+kind, payload, user=u, after_commit=True)` for each permitted user; plus `Notification Log` for `assigned_to` (or permitted users if unassigned) on inbound and on `handoff.requested`.

- [ ] **Step 1: Failing tests:**
  - `test_message_received_creates_conversation_and_channel` (unknown account_id → DB Channel created, Review Focus 1)
  - `test_outgoing_flag_sets_direction_out` and `author_type`: `agent` when message metadata marks AI/agent (field path from fixtures README), else `manager`
  - `test_duplicate_message_is_noop` and `test_duplicate_race_swallowed` (patch insert to raise `frappe.DuplicateEntryError` once → no exception, one row) (Review Focus 2)
  - `test_out_of_order_keeps_latest_preview` (older message processed after newer → preview/last_message_at unchanged)
  - `test_inbound_reopens_closed_conversation`
  - `test_first_response_seconds` (inbound at t, outgoing at t+90s → 90)
  - `test_draft_created_supersedes_previous_pending`
  - `test_handoff_sets_needs_human_and_notifies`
  - `test_message_deleted_marks_deleted`, `test_message_updated_updates_text`
  - `test_realtime_only_to_permitted_users` (patch `frappe.publish_realtime`)
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: webhook event processor`.

### Task 8: Sending replies

**Files:**
- Create: `dialogbrain/api/inbox.py` (send-related methods only in this plan), `dialogbrain/sync/sender.py`
- Modify: `dialogbrain/sync/upsert.py` (echo matching)
- Test: `dialogbrain/tests/test_send.py`

**Interfaces:**
- Consumes: `DialogBrainClient.execute` (Task 2), `assert_can_access` (Task 6), `upsert_message` (Task 7).
- Produces:
  - `@frappe.whitelist() send(conversation: str, text: str, client_ref: str, file_urls: list[str] | None = None) -> dict` → `{"name", "delivery_status"}`; idempotent per `client_ref` (existing row → return it, no new job)
  - `deliver(message_name: str) -> None` (queue `short`): upload files (`files.create_upload_url` → HTTP PUT → `files.complete_upload`; field names from fixtures), `messages.send {thread_id, text, attachments}`, store `message_id` (channel-local id from `data.message_id`; also `db_message_id` if present in `raw`), `delivery_status=sent`; `requests.Timeout` → `unknown` (no automatic retry); other `DialogBrainError` → `failed` + `error`
  - `@frappe.whitelist() retry(message: str) -> None` — only for `failed`
  - Echo match in `upsert_message`: incoming outgoing message with no `message_id` match → find `DB Message` in same conversation with `direction=out`, `delivery_status in (pending, unknown, sent)` and empty or equal `message_id`, identical text, `sent_at` within 120 s → update that row instead of inserting
  - Sending auto-assigns an unassigned conversation to the sender.

- [ ] **Step 1: Failing tests:** `test_send_idempotent_client_ref` (two calls → one row, enqueue once — Review Focus 4); `test_deliver_success_sets_message_id`; `test_deliver_timeout_marks_unknown_no_retry`; `test_deliver_error_marks_failed`; `test_retry_only_failed`; `test_echo_before_message_id_matched` (Review Focus 3); `test_echo_after_message_id_matched`; `test_send_auto_assigns`; `test_send_forbidden_for_restricted_channel`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: send replies through DialogBrain`.

### Task 9: Backfill, reconcile, polling

**Files:**
- Create: `dialogbrain/sync/backfill.py`, `dialogbrain/sync/reconcile.py`
- Modify: `dialogbrain/hooks.py` (`scheduler_events = {"cron": {"* * * * *": ["dialogbrain.sync.reconcile.tick"]}}`)
- Test: `dialogbrain/tests/test_backfill_reconcile.py`

**Interfaces:**
- Consumes: client, `upsert_conversation`, `upsert_message`, `apply_message_counters` (no notifications/realtime for backfilled history: pass `silent=True` — add that kwarg to `upsert_message`/`apply_message_counters` in this task).
- Produces:
  - `run_backfill() -> None` (queue `long`, job id `dialogbrain_backfill`, deduplicated): page `search.threads {since: now - backfill_days, limit: 50, offset}`; per thread page `messages.read_history` backwards until older than `since`; progress in `backfill_status` (`"running 120/340"`, `"done"`, `"error: …"`); resumable (skips messages already stored)
  - `sync_thread(thread_id: str, after: datetime | None) -> int` — read history newer than `after` (newest stored `sent_at`), returns count stored
  - `tick() -> None` — if not connected: return; interval = 60 s (`private`) or 300 s (`public`); skip if `now - last_reconcile_at < interval`; else `reconcile()`
  - `reconcile() -> None` — `search.threads since = last_reconcile_at - 2 min` (paged) → `sync_thread` for threads whose activity is newer than stored; on full success set `last_reconcile_at = start_of_pass`; in public mode also re-activate webhook if `list_webhooks` shows it paused.

- [ ] **Step 1: Failing tests** (client patched with fixture-shaped fakes): `test_backfill_paginates_threads_and_messages`; `test_backfill_is_silent`; `test_backfill_resumes_without_duplicates`; `test_reconcile_fetches_missed_messages`; `test_reconcile_cursor_not_advanced_on_error`; `test_tick_interval_public_vs_private`; `test_reconcile_reactivates_paused_webhook`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: backfill and cursor reconcile`.

### Task 10: Connect / Disconnect / Re-sync and live e2e

**Files:**
- Create: `dialogbrain/setup/connect.py`
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.js` (buttons), `dialogbrain_settings.py`
- Test: `dialogbrain/tests/test_connect.py`

**Interfaces:**
- Produces (all `@frappe.whitelist()`, System Manager only):
  - `connect() -> dict` — 1) `workspace.current` (fails → clear message "Invalid API key or DialogBrain unreachable"); 2) capability check: `channels.list`, `search.threads {limit:1}`, `list_webhooks()` must succeed, else message naming the missing capability; 3) `channels.list` → `upsert_channel` each; 4) if `network_mode == public`: `create_webhook(public_site_url + "/api/method/dialogbrain.api.webhook.receive", EVENTS)` → store id/secret (replace an existing one with the same URL); 5) `connected=1`, `last_reconcile_at=now`; 6) enqueue `run_backfill`. Returns `{"workspace", "channels", "network_mode"}`.
  - `disconnect() -> None` — delete webhook (ignore 404), `connected=0`; keeps mirrored data.
  - `resync() -> None` — enqueue `run_backfill`.
  - `set_network_mode(mode: str) -> None` — switching to private deletes webhook; to public creates it.
  - Settings form: buttons Connect / Disconnect / Re-sync; private mode shows the warning text from the spec ("AI agent can't see ERPNext data — make the site public or use a tunnel").
  - `EVENTS` constant = Global Constraints list.

- [ ] **Step 1: Failing tests** (client patched): `test_connect_happy_path_public`; `test_connect_private_creates_no_webhook`; `test_connect_invalid_key_message`; `test_connect_missing_capability_message`; `test_disconnect_keeps_data`; `test_switch_to_private_deletes_webhook`; `test_connect_requires_system_manager`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS (full suite: `run-tests --app dialogbrain` all green).
- [ ] **Step 5: Live e2e against dialogbrain-dev** (site `frontend`):
  1. Settings: base_url `http://host.docker.internal:8000`, API key (dev workspace 128), network_mode `public`, public_site_url `http://host.docker.internal:8080` → Connect → expect channels listed, `backfill_status` reaches `done`.
  2. `agents_simulate_inbound create_new_livechat=true message_text="Привет, есть ноутбук?"` → within 3 s a new DB Conversation + inbound DB Message on `frontend` (check `DB Webhook Log` status `processed`).
  3. `bench --site frontend execute dialogbrain.api.inbox.send --kwargs '{"conversation": "<name>", "text": "Да, есть", "client_ref": "e2e-1"}'` as Administrator → `delivery_status=sent`, and the echo did not create a second row.
  4. Switch to `private`, simulate another inbound → appears within ~60 s via reconcile.
  Record results (pass/fail per step) in the commit message body.
- [ ] **Step 6: Commit** `feat: connect flow; e2e verified against dialogbrain-dev`.
