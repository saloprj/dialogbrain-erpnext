# DialogBrain for ERPNext — Plan 1: Core & Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An installable Frappe app `dialogbrain` that connects to a DialogBrain workspace with an API key and keeps a complete, permission-checked mirror of all conversations and messages in ERPNext (webhooks + backfill + reconcile, or polling for private sites), can send replies, and links conversations to Customers/Leads.

**Architecture:** Thin HTTP client to DialogBrain (`/api/tools/{tool}/execute`, `/api/v1/webhooks`). Signed webhooks land in a guest endpoint that dedupes and enqueues; one event processor upserts DocTypes (keyed by DialogBrain `db_id`) and publishes realtime events. A scheduler tick runs reconcile on today's API (date + offset paging; 5 min with webhooks, 1 min in polling mode), retries failed webhook deliveries and cleans the log. No UI beyond the Settings form (inbox UI = Plan 2).

**Tech Stack:** Frappe/ERPNext v15+v16 app (Python ≥3.10 syntax), `requests`, Frappe test runner (`FrappeTestCase`), frappe_docker dev stack in `/home/denys/projects/erpnext` (project `erpnext`, `pwd.yml`).

**Spec:** `docs/superpowers/specs/2026-11-13-dialogbrain-erpnext-inbox-design.md` (read §5 "Verified details" before any task).

**Plan series:** 1 Core & Sync (this) → 2 Inbox UI (incl. media via `files.info` by file_id, quick replies, notes, drafts actions) → 3 Agent tools + DialogBrain PRs (signed thread context; `request_id` + `api_key_id` in `/api/tools/{id}/execute`; pause/unpause-agent by API key) → 4 Agent modes, take-over, reports, translations, CI.

## Global Constraints

- App name `dialogbrain`, module `DialogBrain`, repo root `/home/denys/projects/erpnext/dialogbrain_erpnext` (own git repo; commit after every task; never `git stash`).
- Must run on Frappe/ERPNext v15 and v16 → Python 3.10-compatible syntax, no v16-only APIs (CI matrix in Plan 4; locally v16).
- DialogBrain auth: `Authorization: Bearer db_live_…`. Tool calls: `POST {base_url}/api/tools/{tool_id}/execute` body `{"tool_id", "params", "mode": "auto", "skip_approval": true, "thread_id"?}` → `{"success", "data", "error", …}`. No `request_id` passthrough yet.
- **Message identity:** unique key is the DialogBrain global id `db_id` (event `message.db_id`; send response `db_message_id`). `message_id` is the channel ref, stored non-unique. Empty unique fields are stored as `None`, never `""`.
- **Message text = event `html`** (event `text` is truncated to 100 chars).
- `search.threads.since` is a date `YYYY-MM-DD`; `messages.read_history` pages with `limit`/`offset` and filters `date_from`/`date_to` (days, UTC).
- Webhook signature: header `X-DialogBrain-Signature: sha256=<hex>`, HMAC-SHA256 over the raw body, key = `bytes.fromhex(secret.removeprefix("whsec_"))`.
- Webhook responses: 401 only for a bad/missing signature, 413 for body > 1 MB; everything else (including "not connected", duplicates, unknown events) → 200. DialogBrain pauses a subscription after 3 consecutive failed deliveries.
- Events subscribed (`EVENTS`): `message.received`, `message.updated`, `message.deleted`, `conversation.started`, `conversation.updated`, `draft.created`, `handoff.requested`, `contact.updated`. `message.received` fires for outgoing too → direction from `data.message.is_outgoing`.
- Authorship of outgoing: matched to an ERPNext reply → `manager` + `sent_by`; `from_id == "system"` and `from_name not in ("You", "")` → `agent`; otherwise `manager` with empty `sent_by`.
- Notifications: assigned conversation → assignee on every inbound; unassigned → permitted users only on the first inbound of a new/re-opened conversation; `handoff.requested` → always. Only backfill is silent (reconcile notifies).
- Secrets (api_key, webhook_secret) only in `Password` fields; never logged (log `db_live_…abcd`).
- Reconcile interval: 300 s (`public`), 60 s (`private`); `last_reconcile_at` advances only after a fully successful pass. Backfill default 30 days. Webhook Log retention 14 days; failed/stuck rows retried up to 5 attempts.
- Messages ordered by DialogBrain `sent_at`, never by arrival.
- Dev site for e2e: `frontend` (http://localhost:8080; from the dialogbrain-dev api-gateway container it is `http://host.docker.internal:8080` — verified reachable on 2026-11-12). Test site: `test.localhost`.

## Review Focus

1. **Event for an unknown thread or unknown channel account** (channel connected after install, webhook before backfill) → DB Channel and conversation auto-created, nothing dropped. Test in Task 7.
2. **Same message delivered several ways** (webhook retry, reconcile, `message.updated` echo, send response) → exactly one DB Message keyed by `db_id`; a duplicate-key race is swallowed, not a 500. Tests in Tasks 7 and 8.
3. **Message longer than 100 characters** → stored in full (from `html`), and its echo still matches the pending ERPNext reply. Tests in Tasks 7 and 8.
4. **Manager double-clicks Send / browser retries** → one DB Message and one DialogBrain call per `client_ref`; inbound messages (no client_ref) never collide on the unique index. Tests in Tasks 3 and 8.
5. **Phone formats differ** (`380671234508`, `+380 67 123 45 08`, `067-123-45-08`) → matched; but two international numbers from different countries sharing the last 9 digits → NOT matched. Test in Task 5.

---

### Task 1: App scaffold, dev mount, test site

**Files:**
- Create: `pyproject.toml`, `README.md`, `license.txt` (MIT), `.gitignore`
- Create: `dialogbrain/__init__.py` (`__version__ = "0.1.0"`), `dialogbrain/hooks.py`, `dialogbrain/modules.txt` (`DialogBrain`), `dialogbrain/patches.txt`, `dialogbrain/dialogbrain/__init__.py`, `dialogbrain/config/__init__.py`
- Create: `dev/compose.dialogbrain.yml`, `dev/install.sh`
- Test: `dialogbrain/tests/test_smoke.py`

**Interfaces:**
- Produces: importable package `dialogbrain`; `dev/install.sh` (idempotent); the test command every later task uses:
  `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain [--module dialogbrain.tests.<mod>]`

- [ ] **Step 1:** Scaffold like `bench new-app` (pyproject with `flit_core` backend as ERPNext uses; `hooks.py`: `app_name="dialogbrain"`, `app_title="DialogBrain"`, `app_publisher="DialogBrain"`, `required_apps=["erpnext"]`).
- [ ] **Step 2:** `dev/compose.dialogbrain.yml` — override for services `backend, queue-short, queue-long, scheduler, websocket` adding volume `./dialogbrain_erpnext:/home/frappe/frappe-bench/apps/dialogbrain` (compose run from `/home/denys/projects/erpnext`).
- [ ] **Step 3:** `dev/install.sh` (run from `/home/denys/projects/erpnext`):
  1. `sudo docker compose -p erpnext -f pwd.yml -f dialogbrain_erpnext/dev/compose.dialogbrain.yml up -d backend queue-short queue-long scheduler websocket` — named services only, never a bare `up`
  2. in each of those containers: `env/bin/pip install -e apps/dialogbrain -q`
  3. append `dialogbrain` to `sites/apps.txt` if missing
  4. create `test.localhost` if missing: `bench new-site test.localhost --db-root-password admin --admin-password admin --install-app erpnext --mariadb-user-host-login-scope=%`; `bench --site test.localhost set-config allow_tests true`
  5. `bench --site <site> install-app dialogbrain` for `test.localhost` and `frontend` (skip if installed), then `bench --site <site> migrate`.
- [ ] **Step 4: Failing test** `TestSmoke.test_app_installed`: `assert "dialogbrain" in frappe.get_installed_apps()`.
- [ ] **Step 5:** Run `bash dialogbrain_erpnext/dev/install.sh`, then the test with `--module dialogbrain.tests.test_smoke` → PASS.
- [ ] **Step 6: Commit** `feat: scaffold dialogbrain app and dev stack mount`.

### Task 2: DialogBrain HTTP client + recorded fixtures

**Files:**
- Create: `dialogbrain/client.py`, `dialogbrain/tests/utils.py`
- Create: `dialogbrain/tests/fixtures/*.json`, `dialogbrain/tests/fixtures/README.md`
- Test: `dialogbrain/tests/test_client.py`

**Interfaces:**
- Produces:
  - `class DialogBrainError(Exception)` with `.status: int | None`, `.code: str | None`
  - `class DialogBrainClient(base_url: str, api_key: str, timeout: float = 20)`
    - `@classmethod from_settings() -> DialogBrainClient`
    - `execute(tool_id: str, params: dict, thread_id: str | None = None) -> dict | list` — returns `data`; raises `DialogBrainError` on HTTP ≥ 400 or `success == false`
    - `list_tools() -> set[str]` (tool ids available to this key, from the tools list endpoint)
    - `create_webhook(url: str, event_types: list[str]) -> dict` (`{"id", "secret"}`), `delete_webhook(webhook_id: str) -> None`, `activate_webhook(webhook_id: str) -> None`, `list_webhooks() -> list[dict]`, `webhook_deliveries(webhook_id: str, limit: int = 10) -> list[dict]`
  - `mask(secret: str) -> str` → `"db_live_…" + last 4`
  - `load_fixture(name: str) -> dict` in `tests/utils.py`

- [ ] **Step 1: Record fixtures** from dialogbrain-dev (workspace 128; API key provided by the user or created in DialogBrain Settings → Developer): `workspace.current`, `channels.list`, tools list, `search.threads` (`since`=yesterday, `limit=5`), `messages.read_history` (one thread, `limit=5`, and a second page via `offset`), `messages.send` (to a livechat thread from `agents_simulate_inbound create_new_livechat=true`) using a **>100-char text**, webhook bodies for `message.received` (inbound and the outgoing echo of that send), and any `message.updated` that follows. Capture webhook bodies with a temporary logging receiver (a small script on the host). Strip personal data. `fixtures/README.md` must state, with exact JSON paths: thread id (and **which form `messages.send` / `messages.read_history` accept** — channel ref vs `thread.db_id`, verified by calling both), channel type + account id, contact name/phone/email/handle and whether the channel verifies the phone, message `db_id`, `message_id`, `html`, `sent_at`, `is_outgoing`, `from_id`, `from_name`, attachments (`file_id`, …), the per-thread **last-activity field** in `search.threads`, `next_offset` in `read_history`, `db_message_id` in the send response.
- [ ] **Step 2: Failing tests** (patch `requests.Session.request`): `test_execute_posts_tool_body` (URL `…/api/tools/search.threads/execute`, JSON `{"tool_id": "search.threads", "params": {"limit": 5}, "mode": "auto", "skip_approval": True}`, header `Authorization: Bearer db_live_x`); `test_execute_returns_data`; `test_execute_raises_on_success_false` (`.code` from `error`); `test_execute_raises_on_http_401` (`.status == 401`); `test_error_message_never_contains_key`; `test_mask` (`mask("db_live_abcdef1234") == "db_live_…1234"`).
- [ ] **Step 3:** Run → FAIL. **Step 4:** Implement `client.py`. **Step 5:** Run → PASS.
- [ ] **Step 6: Commit** `feat: DialogBrain HTTP client and recorded fixtures`.

### Task 3: DocTypes and roles

**Files:**
- Create doctype folders under `dialogbrain/dialogbrain/doctype/`: `dialogbrain_settings` (Single), `db_channel`, `db_channel_user` (child), `db_conversation`, `db_message`, `db_message_attachment` (child), `db_draft`, `db_webhook_log`
- Create: `dialogbrain/install.py` (`after_install` creates roles); hook `after_install = "dialogbrain.install.after_install"`
- Test: `dialogbrain/tests/test_doctypes.py`

**Interfaces (field names are contracts for all later tasks):**
- `DialogBrain Settings`: `base_url` (Data, default `https://api.dialogbrain.com`), `api_key` (Password), `workspace_id`, `workspace_name` (Data, RO), `network_mode` (Select `public\|private`, default `public`), `public_site_url` (Data), `webhook_id` (Data, RO), `webhook_secret` (Password), `webhook_status` (Small Text, RO), `last_reconcile_at` (Datetime, RO), `backfill_days` (Int, 30), `backfill_status` (Data, RO), `default_company` (Link Company), `connected` (Check, RO)
- `DB Channel` (autoname `field:account_id`): `account_id`, `channel_type`, `title`, `company` (Link Company), `default_manager` (Link User), `agent_mode` (Select `copilot\|autopilot\|off`, default `copilot`), `enabled` (Check, 1), `allowed_users` (Table `DB Channel User`: `user` Link User)
- `DB Conversation` (autoname `hash`): `thread_id` (Data, unique — the form `messages.send` accepts, per fixtures README), `thread_ref` (Data, the other form), `channel` (Link DB Channel), `title`, `contact_name`, `phone`, `email`, `handle`, `avatar_url`, `db_contact_id`, `party_type` (Link DocType), `party` (Dynamic Link), `contact` (Link Contact), `link_source` (Select `\|auto_verified\|auto_unverified\|manual`), `status` (Select `Open\|Closed`, default Open), `assigned_to` (Link User), `agent_override` (Select `default\|human\|agent`, default `default`), `needs_human` (Check), `last_message_at` (Datetime), `last_message_preview` (Small Text), `last_inbound_at` (Datetime), `last_message_direction` (Select `in\|out`), `first_response_seconds` (Int), `unread_count` (Int), `opened_at`, `closed_at` (Datetime)
- `DB Message` (autoname `hash`): `db_id` (Data, unique, `None` while pending), `message_id` (Data, not unique), `conversation` (Link), `direction` (`in\|out`), `author_type` (`customer\|manager\|agent\|system`), `sent_by` (Link User), `text` (Long Text), `sent_at` (Datetime), `attachments` (Table `DB Message Attachment`: `file_id`, `filename`, `mime_type`, `media_type`, `size` — no URL fields), `delivery_status` (`pending\|sent\|failed\|unknown\|deleted`), `client_ref` (Data, unique, `None` for inbound), `error` (Small Text), `raw` (JSON)
- `DB Draft`: `draft_id` (unique), `conversation`, `text`, `created_at`, `status` (`pending\|approved\|rejected\|superseded`)
- `DB Webhook Log`: `delivery_id` (unique), `event`, `payload` (JSON), `received_at`, `status` (`queued\|processed\|failed`), `attempts` (Int), `error`
- Controllers: `DBMessage.validate` converts `""` → `None` for `db_id` and `client_ref`; `on_doctype_update` adds indexes `DB Conversation(status, last_message_at)`, `(assigned_to, status)`, `(phone)`, `(email)`, `DB Message(conversation, sent_at)`.
- Roles `Chat Agent`, `Chat Manager`. DocType perms: System Manager full; Chat Manager read/write all except Settings and Webhook Log; Chat Agent read/write DB Conversation, read DB Message/DB Draft/DB Channel (row filtering = Task 6).

- [ ] **Step 1: Failing tests:** `test_thread_id_unique`; `test_db_id_unique`; `test_client_ref_unique`; `test_two_inbound_without_client_ref` and `test_two_pending_without_db_id` (both insert fine; stored values are `NULL`); `test_roles_exist`; `test_conversation_indexes` (``SHOW INDEX FROM `tabDB Conversation` `` has a key over `status,last_message_at`).
- [ ] **Step 2:** FAIL. **Step 3:** Create DocType JSONs + controllers. **Step 4:** `migrate` both sites, run → PASS. **Step 5: Commit** `feat: dialogbrain doctypes and roles`.

### Task 4: Webhook receiver

**Files:**
- Create: `dialogbrain/sync/__init__.py`, `dialogbrain/sync/signature.py`, `dialogbrain/api/__init__.py`, `dialogbrain/api/webhook.py`
- Test: `dialogbrain/tests/test_webhook.py`

**Interfaces:**
- Consumes: Settings fields (Task 3).
- Produces:
  - `verify_signature(raw_body: bytes, header: str | None, secret: str) -> bool` (`hmac.compare_digest`)
  - `@frappe.whitelist(allow_guest=True, methods=["POST"]) receive()` at `/api/method/dialogbrain.api.webhook.receive` → `{"ok": true}`; stores `DB Webhook Log` (`payload` = parsed body, `status="queued"`) and enqueues `dialogbrain.sync.processor.process_delivery(log_name: str)` on `short`, `enqueue_after_commit=True`.

- [ ] **Step 1: Failing tests:** `test_valid_signature` / `test_invalid_signature` / `test_missing_header` (secret `"whsec_" + "ab"*32`, HMAC computed in the test); `test_receive_rejects_bad_signature` → 401, no log row; `test_receive_rejects_large_body` → 413; `test_receive_dedupes_delivery_id` (same body twice → one row, `frappe.enqueue` patched called once, both 200); `test_receive_when_not_connected_returns_200_and_drops` (no row, no enqueue).
- [ ] **Step 2:** FAIL. **Step 3:** Implement (`frappe.request.get_data()`, size check before parsing, insert with `ignore_permissions`, duplicate `delivery_id` → 200). **Step 4:** PASS. **Step 5: Commit** `feat: signed webhook receiver`.

### Task 5: Party linking

**Files:**
- Create: `dialogbrain/party.py`
- Test: `dialogbrain/tests/test_party.py`

**Interfaces:**
- Produces:
  - `normalize_phone(raw: str | None) -> str | None` — digits only; strip leading `00`; `None` if < 7 digits
  - `phones_match(a: str, b: str) -> bool` — equal normalized digits; OR exactly one side is local format (its digits start with a single `0`), both have ≥ 9 digits and the last 9 digits are equal
  - `find_parties(phone: str | None, email: str | None) -> list[tuple[str, str, str]]` — `(party_type, party, matched_by)` with `matched_by in ("phone","email")`, via `Contact Phone`/`Contact Email` → `Dynamic Link`, plus `Lead.mobile_no/phone/email_id`; Customers first
  - `link_party(conversation: str) -> tuple[str, str] | None` — links only if exactly one Customer (else exactly one Lead) matches; sets `party_type`, `party`, `contact`, `link_source` (`auto_verified` when matched by phone and the conversation's channel type verifies phones per fixtures README — WhatsApp, Viber, Telegram with shared phone — else `auto_unverified`); never overrides `link_source == "manual"`
  - `@frappe.whitelist() set_party(conversation: str, party_type: str, party: str) -> None` — manual link (`link_source="manual"`), adds conversation phone/email to the party's primary Contact if missing (creates Contact if none); calls `assert_can_access` (Task 6, imported inside the function)
  - `@frappe.whitelist() create_lead(conversation: str) -> str`

- [ ] **Step 1: Failing tests:** `test_normalize_phone` (`"+380 67 123 45 08"→"380671234508"`, `"00380671234508"→"380671234508"`, `"123"→None`); `test_match_local_vs_international` (`"067-123-45-08"` vs `"380671234508"` → True); `test_no_match_two_countries_same_tail` (`"48671234508"` vs `"380671234508"` → False); `test_autolink_single_customer_verified` (whatsapp channel → `auto_verified`); `test_autolink_livechat_unverified`; `test_email_match_unverified`; `test_no_autolink_when_two_customers`; `test_lead_when_no_customer`; `test_manual_link_not_overridden`; `test_set_party_adds_phone_to_contact`; `test_create_lead_links_conversation`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement (candidate SQL on `RIGHT(REGEXP_REPLACE(phone,'[^0-9]',''),9)`, then filter in Python with `phones_match`). **Step 4:** PASS. **Step 5: Commit** `feat: link conversations to customers and leads`.

### Task 6: Permissions

**Files:**
- Create: `dialogbrain/permissions.py`; Modify: `dialogbrain/hooks.py` (`permission_query_conditions`, `has_permission` for `DB Conversation`, `DB Message`, `DB Draft`)
- Test: `dialogbrain/tests/test_permissions.py`

**Interfaces:**
- Produces:
  - `allowed_channels(user: str) -> list[str] | None` — `None` = all (System Manager, Chat Manager); Chat Agent → channels with empty `allowed_users` or containing the user
  - `permitted_users(channel: str) -> list[str]` — enabled users with Chat Manager + Chat Agents allowed on the channel
  - `conversation_query(user: str) -> str`, `conversation_has_permission(doc, ptype, user) -> bool`; message/draft variants delegate to their conversation
  - `assert_can_access(conversation: str, user: str | None = None) -> None` (raises `frappe.PermissionError`)

- [ ] **Step 1: Failing tests:** agent sees a conversation on an open channel; agent does not see a channel restricted to another user (`frappe.get_list` and `frappe.has_permission`); manager sees all; messages follow their conversation; `permitted_users` contents; user without chat roles sees nothing; `set_party` on a forbidden conversation raises.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: channel-based permissions`.

### Task 7: Event processor

**Files:**
- Create: `dialogbrain/sync/processor.py`, `dialogbrain/sync/upsert.py`, `dialogbrain/sync/notify.py`
- Test: `dialogbrain/tests/test_processor.py` (fixtures from Task 2)

**Interfaces:**
- Consumes: DocTypes (Task 3); `link_party` (Task 5); `permitted_users` (Task 6).
- Produces:
  - `process_delivery(log_name: str) -> None` — loads the log, `attempts += 1`, routes, sets `processed` / `failed` + `error`
  - `process_event(event: str, data: dict) -> None`
  - `upsert_channel(account_id: str, channel_type: str, title: str | None = None) -> str`
  - `upsert_conversation(thread: dict, channel: str) -> tuple[str, bool]` (name, created) — calls `link_party` on create and on `contact.updated`
  - `upsert_message(conversation: str, message: dict, *, silent: bool = False) -> tuple[str, bool]` (name, created) — key `db_id`; text from `html`; attachments without URLs. Echo matching when no row has this `db_id`: an outgoing message matches a row in the same conversation with `direction="out"`, `db_id is None`, `delivery_status in ("pending","unknown","sent")`, identical full text, `sent_at` within 120 s → update that row (keep `sent_by`, `author_type="manager"`); otherwise insert with authorship per Global Constraints. `frappe.DuplicateEntryError` on insert → re-read and return the existing row.
  - `apply_message_counters(conversation: str, message_name: str, *, silent: bool = False) -> None` — preview, `last_message_at` (never moves backwards), `last_inbound_at`, `last_message_direction`, `unread_count += 1` on inbound, reopen Closed on inbound (`status="Open"`, `opened_at=now`, `first_response_seconds=None`), `first_response_seconds` on the first outgoing after `opened_at`; then `publish(...)` and `notify(...)` unless `silent`
  - `publish(conversation: str, kind: str, payload: dict) -> None` → `frappe.publish_realtime("dialogbrain:" + kind, payload, user=u, after_commit=True)` for each permitted user
  - `notify(conversation: str, reason: str) -> None` — rule from Global Constraints (Notification Log)
  - Event routing: `message.received` and `message.updated` → same upsert path; `message.deleted` → `delivery_status="deleted"`; `conversation.started/updated` → `upsert_conversation`; `contact.updated` → update contact fields + `link_party`; `draft.created` → DB Draft (previous pending → `superseded`); `handoff.requested` → `needs_human=1` + notify; unknown event → `processed`, ignored.

- [ ] **Step 1: Failing tests:** `test_message_received_creates_conversation_and_channel` (unknown account_id); `test_long_message_stored_in_full` (html 300 chars, text 100 → stored 300); `test_direction_and_author_agent` (`from_id="system"`, `from_name="Sales Bot"` → agent); `test_out_from_dialogbrain_ui_is_manager_without_sent_by`; `test_duplicate_db_id_is_noop`; `test_duplicate_race_swallowed` (insert patched to raise `DuplicateEntryError` once); `test_updated_event_for_unknown_message_upserts`; `test_out_of_order_keeps_latest_preview`; `test_inbound_reopens_closed_conversation`; `test_first_response_seconds` (inbound t, outgoing t+90 s → 90); `test_draft_created_supersedes_previous_pending`; `test_handoff_sets_needs_human_and_notifies`; `test_message_deleted_marks_deleted`; `test_realtime_only_to_permitted_users`; `test_notify_unassigned_only_first_inbound` (two inbound → one Notification Log per permitted user); `test_notify_assigned_every_inbound`; `test_silent_upsert_publishes_nothing`; `test_process_delivery_failure_marks_failed`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: webhook event processor`.

### Task 8: Sending replies

**Files:**
- Create: `dialogbrain/api/inbox.py` (send-related methods only in this plan), `dialogbrain/sync/sender.py`
- Test: `dialogbrain/tests/test_send.py`

**Interfaces:**
- Consumes: `DialogBrainClient.execute` (Task 2), `assert_can_access` (Task 6), `upsert_message` echo matching (Task 7).
- Produces:
  - `@frappe.whitelist() send(conversation: str, text: str, client_ref: str, file_urls: list[str] | None = None) -> dict` → `{"name", "delivery_status"}`; idempotent per `client_ref` (existing row → returned, no new job); inserts DB Message `direction="out"`, `author_type="manager"`, `sent_by=session user`, `delivery_status="pending"`, `sent_at=now`; auto-assigns an unassigned conversation to the sender
  - `deliver(message_name: str) -> None` (queue `short`): upload files (`files.create_upload_url` → HTTP PUT → `files.complete_upload`; field names from fixtures README), `messages.send {thread_id, text, attachments}`; on success set `db_id` = response `db_message_id` — if an echo row already holds that `db_id`, merge into the ERPNext row (keep `sent_by`) and delete the echo row; set `message_id`, `delivery_status="sent"`. `requests.Timeout` → `unknown` (no automatic retry; reconcile/echo resolves it); other `DialogBrainError` → `failed` + `error`
  - `@frappe.whitelist() retry(message: str) -> None` — only for `failed`

- [ ] **Step 1: Failing tests:** `test_send_idempotent_client_ref` (two calls → one row, enqueue once); `test_deliver_success_sets_db_id`; `test_echo_arrives_before_response_merged` (echo processed between insert and response → one row, `sent_by` kept); `test_echo_after_response_matched_by_db_id`; `test_long_text_echo_matched` (250-char text); `test_deliver_timeout_marks_unknown_no_retry`; `test_deliver_error_marks_failed`; `test_retry_only_failed`; `test_send_auto_assigns`; `test_send_forbidden_for_restricted_channel`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: send replies through DialogBrain`.

### Task 9: Backfill, reconcile, polling, log maintenance

**Files:**
- Create: `dialogbrain/sync/backfill.py`, `dialogbrain/sync/reconcile.py`, `dialogbrain/sync/maintenance.py`
- Modify: `dialogbrain/hooks.py` — `scheduler_events = {"cron": {"* * * * *": ["dialogbrain.sync.reconcile.tick"]}, "daily": ["dialogbrain.sync.maintenance.cleanup_webhook_log"]}`
- Test: `dialogbrain/tests/test_backfill_reconcile.py`

**Interfaces:**
- Consumes: client; `upsert_channel`, `upsert_conversation`, `upsert_message`, `apply_message_counters` (Task 7).
- Produces:
  - `run_backfill() -> None` (queue `long`, `job_id="dialogbrain_backfill"`, deduplicated): page `search.threads {since: <today − backfill_days>, limit: 50, offset}`; per thread page `messages.read_history {thread_id, date_from: <same date>, limit: 100, offset}` until `next_offset` is absent; `silent=True`; progress in `backfill_status` (`"running 120/340"`, `"done"`, `"error: …"`); reruns are cheap (upsert by db_id)
  - `sync_thread(thread_id: str, date_from: str) -> int` — reads all pages from `date_from`, upserts (not silent), returns number of new rows
  - `reconcile() -> None` — `start = now`; `since = (last_reconcile_at − 1 day).date()`; page `search.threads {since, limit: 50, offset}`; for each thread whose last-activity timestamp (fixtures README) is newer than our newest stored `sent_at` for that conversation (or the conversation is unknown) → `sync_thread(thread_id, <date of our newest sent_at, or since>)`; after a fully successful pass `last_reconcile_at = start`; in `public` mode, if `list_webhooks()` shows our subscription inactive → `activate_webhook` and record it in `webhook_status`
  - `retry_webhook_logs() -> None` — re-enqueue `DB Webhook Log` rows with `status="failed"` or (`status="queued"` and `received_at` older than 10 min), `attempts < 5`
  - `tick() -> None` — not connected → return; `retry_webhook_logs()`; enqueue `reconcile` (`queue="long"`, `job_id="dialogbrain_reconcile"`, `deduplicate=True`) when `now − last_reconcile_at ≥ 300 s` (`public`) or `≥ 60 s` (`private`)
  - `cleanup_webhook_log() -> None` — delete rows older than 14 days

- [ ] **Step 1: Failing tests** (client patched with fixture-shaped fakes; assert real param shapes — `since` matches `^\d{4}-\d{2}-\d{2}$`, `offset` increments): `test_backfill_paginates_threads_and_messages`; `test_backfill_is_silent`; `test_backfill_rerun_no_duplicates`; `test_reconcile_fetches_missed_messages` (and notifies — not silent); `test_reconcile_skips_unchanged_threads`; `test_reconcile_cursor_not_advanced_on_error`; `test_reconcile_after_long_outage_uses_older_since`; `test_tick_interval_public_vs_private`; `test_reconcile_reactivates_paused_webhook`; `test_retry_failed_and_stuck_logs` (attempts cap 5); `test_cleanup_webhook_log_14_days`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5: Commit** `feat: backfill, reconcile, webhook log maintenance`.

### Task 10: Connect / Disconnect / Re-sync, webhook health, live e2e

**Files:**
- Create: `dialogbrain/setup/__init__.py`, `dialogbrain/setup/connect.py`
- Modify: `dialogbrain/dialogbrain/doctype/dialogbrain_settings/dialogbrain_settings.js`, `dialogbrain_settings.py`
- Test: `dialogbrain/tests/test_connect.py`

**Interfaces:**
- Produces (`@frappe.whitelist()`, System Manager only):
  - `REQUIRED_TOOLS = {"workspace.current", "channels.list", "search.threads", "messages.read_history", "messages.send", "files.create_upload_url", "files.complete_upload"}`; `EVENTS` = Global Constraints list
  - `connect() -> dict` — 1) `workspace.current` (error → "Invalid API key or DialogBrain unreachable"); 2) `REQUIRED_TOOLS - client.list_tools()` must be empty and `list_webhooks()` must succeed, else a message naming what is missing; 3) `channels.list` → `upsert_channel` each; 4) `public` mode: `create_webhook(public_site_url + "/api/method/dialogbrain.api.webhook.receive", EVENTS)` (delete an existing subscription with the same URL first) → store id/secret; 5) `connected=1`, `last_reconcile_at=now`; 6) enqueue `run_backfill`. Returns `{"workspace", "channels", "network_mode"}`
  - `disconnect() -> None` — delete webhook (ignore 404), `connected=0`; mirrored data kept
  - `resync() -> None` — enqueue `run_backfill`
  - `set_network_mode(mode: str) -> None` — to `private`: delete webhook; to `public`: create it
  - `webhook_health() -> dict` — `{"active", "last_delivery_at", "last_status", "failures"}` from `webhook_deliveries` + `list_webhooks`; refreshes `webhook_status`
  - Settings form: buttons Connect / Disconnect / Re-sync / Check webhook; a health line under the webhook fields (warn when inactive or last deliveries failed); in `private` mode the warning "AI agent can't see ERPNext data — make the site public or use a tunnel".

- [ ] **Step 1: Failing tests** (client patched): `test_connect_happy_path_public`; `test_connect_private_creates_no_webhook`; `test_connect_invalid_key_message`; `test_connect_missing_tool_message` (names `messages.send`); `test_connect_replaces_existing_subscription_same_url`; `test_disconnect_keeps_data`; `test_switch_to_private_deletes_webhook`; `test_webhook_health_paused`; `test_connect_requires_system_manager`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** full suite `run-tests --app dialogbrain` → all green.
- [ ] **Step 5: Live e2e against dialogbrain-dev** (site `frontend`):
  1. Settings: base_url `http://host.docker.internal:8000`, dev API key, `public`, public_site_url `http://host.docker.internal:8080` → Connect → channels listed, `backfill_status` reaches `done`, Check webhook → active.
  2. `agents_simulate_inbound create_new_livechat=true message_text="Привет, есть ноутбук?"` → within 3 s a DB Conversation + inbound DB Message on `frontend`; `DB Webhook Log` row `processed`.
  3. `bench --site frontend execute dialogbrain.api.inbox.send --kwargs '{"conversation": "<name>", "text": "<a 150-character reply>", "client_ref": "e2e-1"}'` → `delivery_status="sent"`, `db_id` set, the echo did not create a second row, text stored in full.
  4. Switch to `private`, simulate another inbound → appears within ~60 s via reconcile, with a Notification Log.
  Record pass/fail per step in the commit body.
- [ ] **Step 6: Commit** `feat: connect flow and webhook health; e2e verified against dialogbrain-dev`.
