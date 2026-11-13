# Plan 2 — Inbox UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Managers do their whole daily chat work inside ERPNext. The Desk page "Chats" (`/app/dialogbrain-inbox`) has three panes: the conversation list, the conversation with a composer, and the customer card. It works on top of the Plan 1 mirror.

**Architecture:**
- Inbox server API: whitelisted methods in `dialogbrain/api/`. Every method checks role and conversation visibility server-side and returns plain dicts shaped for the UI.
- UI: one Frappe Desk Page, hosting a Vue 3 app built by Frappe's esbuild from `dialogbrain/public/js/inbox/*.bundle.js`.
- Live updates: the UI subscribes to the existing `dialogbrain:*` realtime events and re-fetches what changed.
- Media: proxied, never copied.

**Tech Stack:** Frappe/ERPNext v15+v16; Vue 3 (Frappe's own `vue` dependency); `frappe.call`; `frappe.realtime`; Python 3.10.

**Spec:** `docs/superpowers/specs/2026-11-13-dialogbrain-erpnext-inbox-design.md`. Read §1, §2 (goals 2–5), §7.1 (DB Quick Reply, DB Note, roles), §7.5 (sending, drafts, media) and §7.7 (UI layout and realtime). Plan 1 follow-ups: `docs/superpowers/plans/2026-11-13-plan-1-followups.md`.

## Global Constraints

**Repo and process**
- Branch `feat/plan-2-inbox`, based on `feat/oauth-connect`. Repo `/home/denys/projects/erpnext/dialogbrain_erpnext`.
- Commit after every task; never `git stash`. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Python 3.10, no v16-only Frappe APIs. Use `frappe.cache()` in the callable form. Tests subclass `dialogbrain.tests.utils.DialogBrainTestCase`.
- Running tests:
  - one module: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain --module dialogbrain.tests.<module>`;
  - full suite: `--app dialogbrain`.
- Building assets: `sudo docker exec erpnext-backend-1 bench build --app dialogbrain`, then restart backend and frontend: `sudo docker restart erpnext-backend-1 erpnext-frontend-1`. Check container names with `sudo docker ps`.

**Access rules (server-side, every method)**
- Every inbox method calls `dialogbrain.permissions.assert_can_access(conversation)` for anything about a conversation.
- Lists use `frappe.get_list` / `frappe.qb` with the user's permission query: `permission_query_conditions` is already registered.
- Never trust ids sent by the browser without that check.
- Chat Agent:
  - may assign a conversation only to themself, and only when it is unassigned or already theirs;
  - may close and reopen;
  - may mark read;
  - may link a party, via the existing `party.set_party` / `party.create_lead`;
  - may add notes;
  - may use quick replies.
- Chat Manager and System Manager may also assign anyone and manage shared quick replies.
- Level-1 DB Conversation fields (everything except `status`) are written only by these server methods. Plan 1 fix F5 locks them for agents in the desk.

**Status semantics**
- Close: set `closed_at = now()`.
- Reopen, by a manager or by a new inbound, which is already in Plan 1: set `opened_at = now()` and clear `closed_at`.
- Publish `dialogbrain:conversation` after each change.

**Contracts with Plan 1 (do not change)**
- `dialogbrain.api.inbox.send(conversation, text, client_ref, file_urls)` returns `{name, delivery_status}`.
- `dialogbrain.api.inbox.retry(message)`.
- `party.set_party(conversation, party_type, party)` and `party.create_lead(conversation)`.
- Realtime events go to permitted users. Each is named `dialogbrain:<kind>`, and its payload always includes `conversation`:
  - `conversation` (any change; may carry `needs_human`);
  - `message` (`message`, `direction`);
  - `message_updated` (`message`);
  - `message_removed` (`message`);
  - `draft` (`draft`).
- DB Message `delivery_status`: inbound is NULL, outbound is one of `pending | sent | failed | unknown | deleted`.
- DB Message `author_type`: one of `customer | manager | agent | system`.

**Messages**
- Ordered by `sent_at`, then `creation`, never by arrival.
- Message text is stored HTML from DialogBrain. Render it only after `frappe.utils.xss_sanitise` on the server, or as text. Never inject unsanitised HTML.
- All user-visible strings in JS are wrapped in `__()`. Python strings are wrapped in `_()`. Translations themselves are Plan 4.

**Media**
- `dialogbrain.api.media.get(message, file_id)` streams the file from DialogBrain through ERPNext.
- Checks: access to the message's conversation, and `file_id` belongs to that message's attachments.
- Size cap: `MEDIA_MAX_BYTES = 50 * 1024 * 1024`. Larger files are refused with 413.
- Response headers: `Cache-Control: private, max-age=300`, and `Content-Disposition` with the filename.
- `file_id` is resolved to a fresh download URL on demand. The plan builder checks which DialogBrain tool returns it (`files.info`, else `files.read`) against `~/projects/dialogbrain` and records the choice in the module docstring.

**Paging**
- Conversation list: 50 per page, ordered by `last_message_at desc, name desc`. A keyset cursor is the `(last_message_at, name)` of the last row.
- Messages: 50 per page, newest first from the server and reversed in the UI. Cursor `before = (sent_at, name)`.

**Live e2e**
- Run only on the `frontend` site, against the local dev DialogBrain, workspace **Test User (128)**.
- Inbound test messages come only from a fresh livechat visitor via the public widget API. Find the widget in Plan 1's Task 10 report or the fixtures README.
- Never message real threads.
- Never print `dev/dialogbrain-dev.env`.

## Review Focus

1. **A Chat Agent calls an inbox method with another channel's conversation name** (crafted request). Expected: 403 / PermissionError, and nothing is returned or changed. Tests in Tasks 1–3.
2. **An agent assigns a conversation to a colleague, or takes one already assigned to someone else.** Expected: refused. A manager can do both. Test in Task 1.
3. **A message whose HTML contains `<script>`/`onerror=`** is rendered safely in the conversation pane and in the list preview. Test in Task 1 (server sanitising) and a check in Task 6.
4. **Two managers have the same conversation open and one replies.** Expected: the other sees the reply appear without reloading, and their unsent composer text is kept. Check in Task 7.
5. **A media request for a `file_id` that belongs to a different conversation's message.** Expected: refused, even when the user can see the message they named. Test in Task 3.

---

### Task 1: Inbox read & action API

**Files:**
- Create: `dialogbrain/api/conversations.py`
- Modify: `dialogbrain/permissions.py`. Add `is_manager(user=None) -> bool` (True for Chat Manager or System Manager), only if it does not already exist.
- Test: `dialogbrain/tests/test_inbox_api.py`

**Interfaces** (all `@frappe.whitelist()`; mutating methods use `methods=["POST"]`):

- `list_conversations(filters: dict | str | None = None, cursor: dict | str | None = None, limit: int = 50) -> {"rows": [...], "next_cursor": dict | None}`
  - Filter keys:
    - `status`: `"Open" | "Closed" | "All"`, default Open;
    - `scope`: `"all" | "mine" | "unassigned"`;
    - `unanswered`: bool (last message inbound and status Open);
    - `channel`: DB Channel name;
    - `channel_type`: e.g. `"telegram"`;
    - `assigned_to`: user, managers only, ignored for agents;
    - `search`: matches title, contact_name, phone, email or handle with LIKE; for ≥3 chars, also DB Message text in the last 90 days via EXISTS.
  - Each row has: `name, title, contact_name, channel, channel_type, channel_title, avatar_url, assigned_to, assigned_to_name, status, unread_count, needs_human, last_message_at, last_message_preview` (plain text, max 120 chars), `last_message_direction, party_type, party`.
- `counters() -> {"unread_total": int, "unanswered": int, "mine": int, "unassigned": int, "needs_human": int}`. Counts Open conversations visible to the user; `unread_total` is the sum of `unread_count`.
- `get_conversation(name) -> dict`. Returns:
  - the row fields above;
  - `phone, email, handle, opened_at, closed_at, first_response_seconds, link_source, agent_override, contact`;
  - `channel: {name, title, channel_type, agent_mode}`;
  - `permissions: {can_assign_any: bool}`.
- `get_messages(conversation, before: dict | str | None = None, limit: int = 50) -> {"rows": [...], "next_before": dict | None}`
  - Each row has: `name, direction, author_type, sent_by, sent_by_name, html` (sanitised), `text` (plain), `sent_at, delivery_status, error` (if failed), and `attachments: [{file_id, filename, mime_type, media_type, size}]`.
- `get_messages_by_name(conversation, names: list) -> rows`. Used by realtime refresh. Every name must belong to the conversation.
- `mark_read(conversation)`: sets `unread_count = 0` and publishes `conversation`.
- `assign(conversation, user: str | None)`
  - Agent: only `user == session.user`, and only when the conversation is unassigned or already theirs. `user=None` unassigns only their own.
  - Manager: anyone, or None.
  - The target user must have Chat Agent or Chat Manager and be able to see the channel (`permitted_users`).
  - Publishes `conversation`.
- `set_status(conversation, status: "Open" | "Closed")`: follows the Status semantics, then publishes.
- `get_drafts(conversation) -> [{name, draft_id, text, created_at}]`. Pending only, newest first.

- [ ] **Step 1: Write failing tests** in `test_inbox_api.py`. Fixtures: two channels (A open to everyone, B restricted to `agent_b`); users `agent_a` (Chat Agent), `agent_b` (Chat Agent) and `mgr` (Chat Manager); conversations and messages created via the Plan 1 upsert helpers or direct inserts.
  - `test_list_scopes_and_filters`: status, mine, unassigned, unanswered, channel and channel_type each return exactly the expected names.
  - `test_list_paging_keyset`: 120 conversations give 3 pages with no overlap or gaps; ordered by `last_message_at desc`.
  - `test_list_search_by_phone_and_text`.
  - `test_agent_cannot_see_restricted_channel`: `list_conversations` / `get_conversation` / `get_messages` / `mark_read` / `assign` / `set_status` on a channel-B conversation as `agent_a` raise `frappe.PermissionError` (Review Focus 1).
  - `test_agent_assign_rules` (Review Focus 2):
    - self on unassigned → ok;
    - self on a conversation assigned to someone else → refused;
    - assigning a colleague → refused;
    - unassigning their own → ok.
  - `test_manager_assigns_anyone`: also refuses a target user who can't see the channel.
  - `test_close_and_reopen_set_timestamps`.
  - `test_mark_read_zeroes_unread`.
  - `test_messages_ordered_by_sent_at_and_paged`: insert out of order and check.
  - `test_message_html_sanitised`: `<p>hi</p><script>x</script><img src=x onerror=y>` comes back with no `script` and no `onerror`; `last_message_preview` is plain text (Review Focus 3).
  - `test_counters_match_list`.
  - `test_get_messages_by_name_rejects_foreign_names`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Reuse `assert_can_access`, `permitted_users`, `allowed_channels`. Use `frappe.utils.xss_sanitise` (or `frappe.utils.html_utils.sanitize_html`, whichever exists on both v15 and v16; check) for `html`, and `frappe.utils.strip_html` for text. The search must not interpolate user input into SQL; use parameters / qb.
- [ ] **Step 4: Run, expect PASS. Run the full suite.**
- [ ] **Step 5: Commit** `feat: inbox read and action API`.

### Task 2: Notes, quick replies, drafts

**Files:**
- Create DocTypes under `dialogbrain/dialogbrain/doctype/`:
  - `db_note`: `conversation` Link DB Conversation (reqd), `text` Small Text (reqd). Standard `owner`/`creation` are the author and time. Permissions: Chat Agent and Chat Manager can create and read through `has_permission` / `permission_query_conditions` reusing `_via_conversation`; owner-only delete.
  - `db_quick_reply`: `title` Data (reqd), `shortcut` Data (unique per owner, e.g. `/price`), `text` Text (reqd), `company` Link Company (optional), `shared` Check. Permissions:
    - Chat Manager has full rights;
    - Chat Agent can create/read/write/delete their own;
    - everyone with Chat Agent can read shared ones;
    - only Chat Manager/System Manager may set `shared=1` (enforced in `validate`).
- Modify `dialogbrain/hooks.py` with the permission hooks for DB Note and DB Quick Reply.
- Create `dialogbrain/api/composer.py`.
- Test: `dialogbrain/tests/test_composer_api.py`.

**Interfaces:**
- `list_notes(conversation)` → `[{name, text, owner, owner_name, creation}]`. `add_note(conversation, text)` (POST, publishes `conversation`). `delete_note(note)` (POST; owner or manager).
- `list_quick_replies(conversation)` → `[{name, title, shortcut, text}]`. This is the user's own plus shared ones; company filter: the reply's company is empty or equals the channel's company. `text` is rendered with placeholders:
  - `{customer_name}`: the party name, else `contact_name`;
  - `{manager_name}`: the session user's full name;
  - `{company}`: the channel's company.
- `approve_draft(draft, text: str | None = None)` (POST)
  - Calls DialogBrain `agents.approve_draft` with `{"draft_id": <draft_id>, "edited_text": text}`. Omit `edited_text` when `text` is None or unchanged. Verify the param names against the tool catalog (`client.list_tools()`) and `~/projects/dialogbrain`.
  - Marks the DB Draft `approved` and publishes `draft`.
  - The sent message arrives through the normal webhook/echo path; do not insert it here.
  - On DialogBrain error: `frappe.throw` with a safe message, and the draft stays pending.
- `reject_draft(draft)` (POST): `agents.reject_draft` → `rejected`.
- Draft text: Plan 1 stores up to 500 chars from the webhook. `get_full_draft(draft)` fetches the full text via `agents.list_drafts` (filter by thread/draft id) when the stored text is exactly 500 chars, updates the row and returns it.
- Every method: `assert_can_access` on the draft's or note's conversation.

- [ ] **Step 1: Write failing tests:**
  - note add/list/delete, plus an agent from another channel refused;
  - quick replies:
    - own and shared visible, another agent's private not visible;
    - placeholders rendered;
    - agent cannot set `shared=1`;
    - company filter;
  - `approve_draft`: patch the DialogBrain client; check params, status and the publish call;
  - `reject_draft`;
  - DialogBrain error keeps the draft pending;
  - `get_full_draft` fetches only at 500 chars;
  - permission refusal on a foreign draft.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Migrate both sites.
- [ ] **Step 4: Run, expect PASS. Run the full suite.**
- [ ] **Step 5: Commit** `feat: notes, quick replies and draft actions`.

### Task 3: Media proxy and composer uploads

**Files:**
- Create: `dialogbrain/api/media.py`.
- Test: `dialogbrain/tests/test_media.py`.

**Interfaces:**
- `@frappe.whitelist(methods=["GET"]) get(message, file_id)`
  - Checks:
    - `assert_can_access` on the message's conversation;
    - `file_id` is one of that message's `DB Message Attachment.file_id`, otherwise PermissionError (Review Focus 5).
  - Resolves a download URL through the DialogBrain client: the tool chosen per Global Constraints, re-resolved on every request.
  - Streams with `requests.get(url, stream=True, timeout=30)` into `frappe.local.response` as `type="download"`-like binary. Refuses if `Content-Length` > `MEDIA_MAX_BYTES`, or if the stream exceeds it.
  - Headers: Content-Type from the attachment's `mime_type` (fallback from the response), `Cache-Control: private, max-age=300`. Content-Disposition is `inline` for image/audio/video and `attachment` otherwise.
  - Errors from DialogBrain become a 502 with a short message; no URL in the message.
- Composer uploads keep using Frappe File: the UI uploads with `frappe.ui.FileUploader`-compatible `upload_file` (private) and passes `file_urls` to `send`. Plan 1's `send` already accepts `file_urls`. No server change unless a test shows a gap.

- [ ] **Step 1: Write failing tests:**
  - happy path (patched client + patched `requests`; checks headers and body);
  - foreign `file_id` refused;
  - no access refused;
  - oversize refused (Content-Length) and oversize while streaming refused;
  - DialogBrain error → 502;
  - no URL leaked.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS. Run the full suite.**
- [ ] **Step 5: Commit** `feat: media proxy for message attachments`.

### Task 4: Customer card API

**Files:**
- Create: `dialogbrain/api/party_card.py`.
- Test: `dialogbrain/tests/test_party_card.py`.

**Interfaces:**
- `get_card(conversation)` returns:
  - `party_type, party, party_name, link_source`;
  - `contact: {name, phone, email}`;
  - `stats: {orders_count, orders_total, currency}`, from submitted Sales Orders of the Customer;
  - `recent_orders`: the last 5 Sales Orders, as `{name, transaction_date, grand_total, status, currency}`;
  - `unpaid_invoices`: submitted Sales Invoices with `outstanding_amount > 0`, max 5, as `{name, due_date, outstanding_amount, currency}`;
  - for a Lead: `lead: {name, status, company_name}`, with no orders;
  - `suggestions`: the result of `party.find_parties(phone, email)` when no party is linked.
- ERPNext permissions apply to the order and invoice data:
  - use `frappe.get_list` for Sales Order / Sales Invoice, so a manager without Sales User sees `stats: null` and no lists, instead of an error;
  - `has_access` flags in the response tell the UI what to hide.
- `search_parties(conversation, txt)` returns up to 10 Customers and Leads matching name/phone/email, via `frappe.get_list` with permissions.
- New-document prefill helpers (client-side, documented here for Task 6): Sales Order / Quotation open through `frappe.new_doc(doctype, {customer | party_name, ...})`. Lead creation uses the existing `party.create_lead`.

- [ ] **Step 1: Write failing tests:**
  - customer with 2 submitted SOs and 1 unpaid SI → stats and lists correct;
  - draft SO not counted;
  - Lead card;
  - unlinked card with suggestions;
  - user without Sales Order read → `stats` null with no exception;
  - conversation access refused.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS. Run the full suite.**
- [ ] **Step 5: Commit** `feat: customer card API for the inbox`.

### Task 5: Desk page and Vue build skeleton

**Files:**
- Create Page `dialogbrain/dialogbrain/page/dialogbrain_inbox/`:
  - `dialogbrain_inbox.json`: title "Chats", roles Chat Agent, Chat Manager, System Manager;
  - `dialogbrain_inbox.js`: mounts the bundle with `frappe.require("dialogbrain_inbox.bundle.js")` into the page body.
- Create `dialogbrain/public/js/inbox/dialogbrain_inbox.bundle.js`. Its entry does `createApp(App).mount(el)`, exported as `frappe.dialogbrain.mountInbox(el, opts)`.
- Create `dialogbrain/public/js/inbox/App.vue` with three panes, using placeholder components `ConversationList.vue`, `ConversationView.vue` and `CustomerCard.vue`.
- Create `dialogbrain/public/js/inbox/api.js`: thin wrappers around `frappe.call` for every Task 1–4 method.
- Create `dialogbrain/public/js/inbox/store.js`: a reactive store holding selected conversation, filters, list rows, messages by conversation, and counters.
- Modify `dialogbrain/hooks.py`: add a workspace shortcut and the "Chats" link if the workspace JSON exists. Otherwise create a minimal Workspace "DialogBrain" with shortcuts to Chats and DialogBrain Settings.

**Interfaces:**
- Route `/app/dialogbrain-inbox`, with an optional `?conversation=<name>` deep link.
- Layout and mobile behaviour:
  - Width ≥ 1200 px: three columns, 360 px | flexible | 320 px.
  - 768–1199 px: the customer card becomes a toggleable drawer.
  - Below 768 px: a single pane with back navigation.
- CSS lives scoped in the components. Use Frappe CSS variables (`--bg-color`, `--text-color`, `--border-color`, …) so light and dark desk themes both work.

- [ ] **Step 1: Implement the skeleton.**
- [ ] **Step 2: Build and check it renders.** `bench build --app dialogbrain` must succeed and the page must render the three empty panes:
  - Confirm the `import { createApp } from "vue"` resolution works on this v16 bench. If it fails, record the fix in the commit: an app `package.json` with `vue` matching frappe's version, or the import path frappe exposes.
  - Restart the frontend and backend.
  - Open `http://localhost:8080/app/dialogbrain-inbox` in a browser (Administrator / admin — local dev site test credentials). Screenshot it, and check the console has no errors from the bundle. If no browser tool is available, report that and the controller checks.
- [ ] **Step 3: Run the full suite** (Python, unchanged).
- [ ] **Step 4: Commit** `feat: Chats desk page with Vue skeleton`.

### Task 6: List, conversation view, composer, customer card

**Files:**
- Modify/Create under `dialogbrain/public/js/inbox/`:
  - `ConversationList.vue`, `ListFilters.vue`, `ConversationRow.vue`;
  - `ConversationView.vue`, `MessageBubble.vue`, `AttachmentView.vue`, `Composer.vue`, `DraftBar.vue`, `ConversationHeader.vue`;
  - `CustomerCard.vue`, `NotesPanel.vue`.

**Behaviour:**
- **List**
  - Filter row:
    - status select (Open/Closed/All);
    - manager select (managers only);
    - search with a 300 ms debounce.
  - Chips:
    - All, plus one per channel_type present in the visible channels;
    - Unanswered(n), Mine(n), Unassigned(n) and Needs human(n), with counts from `counters()`.
  - Rows show:
    - avatar or initials, name, channel icon or label;
    - assignee name;
    - plain-text preview;
    - time (today `HH:mm`, else date);
    - unread badge;
    - a red marker when `needs_human`.
  - Infinite scroll uses `next_cursor`. Selecting a row loads the conversation and calls `mark_read`.
- **Conversation**
  - Header:
    - name · channel · phone;
    - Open/Close button (`set_status`);
    - Assign dropdown: agents see "Assign to me" / "Unassign"; managers see users from `permitted_users`, which needs a small `assignable_users(conversation)` API added to `conversations.py` together with its test;
    - linked party chip.
  - Messages:
    - inbound left, outbound right;
    - an "AI" badge when `author_type == "agent"`; a muted style for `system`;
    - `sent_by_name` for manager replies.
  - Rendering:
    - message `html` is already sanitised by the server, and is inserted with `v-html` only after the server sanitising;
    - delivery status icons: pending clock, sent check, unknown "?" with a tooltip, failed red with a "Retry" button → `retry`;
    - attachments: images become thumbnails via the media proxy URL (`/api/method/dialogbrain.api.media.get?message=…&file_id=…`), opened full-size on click; `audio/*` gets an `<audio controls>`; `video/*` gets `<video controls>`; other files get a download link;
    - "Load earlier" uses `next_before`;
    - auto-scroll to the bottom on open and on a new message only when the user is already near the bottom.
- **DraftBar**: for each pending draft, show the text plus Approve / Edit / Reject.
  - Edit puts the text into the composer in "editing draft" mode; Send then calls `approve_draft(draft, text)`.
  - When the text is 500 chars, call `get_full_draft` first.
- **Composer**
  - Textarea: Enter sends, Shift+Enter makes a newline.
  - Attach files via `upload_file` (private), with a chip per file and a remove button.
  - A quick-replies picker opens on the ⚡ button or when the user types `/`, and filters by shortcut/title.
  - Send:
    - generates `client_ref = crypto.randomUUID()` once per message;
    - calls `send`;
    - shows the message optimistically as pending;
    - replaces it on the `message` / `message_updated` realtime events.
  - The composer text is kept per conversation in the store, so switching conversations never loses it.
- **CustomerCard**
  - Contact block and party block.
  - If not linked: suggestions with "Link", "Link to existing…" (search via `search_parties`) and "Create Lead".
  - If Customer: stats, recent orders and unpaid invoices, each linking to its form, plus buttons "Create Sales Order" and "Create Quotation" using `frappe.new_doc` with prefill.
  - If Lead: lead info, "Create Quotation", and "Open Lead".
  - NotesPanel: list and add notes.

- [ ] **Step 1: Implement the components.**
- [ ] **Step 2: Build, then check in a browser on `frontend`** (Administrator):
  - the list loads with real mirrored conversations and the filters change it;
  - a conversation shows messages with correct alignment and AI badges;
  - an image attachment shows through the proxy (if any exists in the data);
  - the customer card shows a linked customer, where one exists (the seed customer "Иван Петров" has orders — link a test livechat conversation to it in Task 8);
  - no console errors;
  - screenshots at 1440 px and 390 px width.
- [ ] **Step 3: Commit** `feat: inbox list, conversation view, composer and customer card`.

### Task 7: Realtime, badges, notifications, form links

**Files:**
- Create `dialogbrain/public/js/inbox/realtime.js`, which subscribes:
  - `dialogbrain:conversation`: refresh that row and the counters. If it is the open conversation, refresh the header and card. The refresh is debounced at 300 ms per conversation.
  - `dialogbrain:message` / `message_updated` / `message_removed`: when the conversation is open, fetch via `get_messages_by_name` and merge by name (replacing an optimistic pending row by `client_ref`). Otherwise refresh the row.
  - `dialogbrain:draft`: refresh the drafts of the open conversation.
  - Unsubscribes when the page is destroyed.
- Create `dialogbrain/public/js/dialogbrain_desk.bundle.js`, included through `app_include_js` in `hooks.py`:
  - a navbar badge: a "Chats" icon with `unread_total` from `counters()`, refreshed on `dialogbrain:conversation` events and every 60 s as a fallback;
  - only for users with Chat Agent, Chat Manager or System Manager;
  - when a `dialogbrain:message` arrives with `direction == "in"`, the inbox page is not focused, and the user is permitted (they got the event), play a short sound. Ship `dialogbrain/public/sounds/new_message.mp3`, under 30 KB, a self-made or CC0 tone recorded in the commit message. Respect a user toggle stored in `localStorage` (try/catch).
- Customer and Lead forms (`doctype_js` in `hooks.py`): a "Chats" section listing linked conversations (`list_conversations` with a `party` filter — add the `party_type` + `party` filter keys to Task 1's API with a test). Each one links to `/app/dialogbrain-inbox?conversation=<name>`.

- [ ] **Step 1: Write a failing test** for the new `party` filter in `test_inbox_api.py`, then implement it.
- [ ] **Step 2: Implement realtime.js, the desk bundle and the form scripts.** Build, then check in a browser:
  - **Review Focus 4.** Open two tabs as different users who both see the livechat channel: Administrator, plus `e2e-agent@example.com` (from Plan 1 Task 10; set a password for it via `bench --site frontend set-password` with a generated value written to the scratchpad, never into the repo). Send a reply from one tab. It must appear in the other without reload, and the other tab's composer draft text must stay.
  - The badge updates.
  - The Customer form shows its Chats.
- [ ] **Step 3: Run the full suite. Commit** `feat: realtime inbox updates, unread badge, sound and form links`.

### Task 8: Live e2e and polish

**Files:** none beyond fixes found. Record the results in the body of an empty commit `test: inbox e2e against dev DialogBrain`.

- [ ] **Step 1:** Send a new message as a fresh livechat visitor through the public widget API. Check:
  - it appears at the top of the list within ~3 s, with an unread badge, the navbar badge and the sound;
  - opening it clears unread.
- [ ] **Step 2:** Reply from the composer with text plus an image file. Check:
  - the status goes pending → sent;
  - the echo doesn't duplicate;
  - the visitor receives the message (check via the widget API / `read_history`).
- [ ] **Step 3:** Assign to self, close and reopen. Check the counters and filters reflect each change.
- [ ] **Step 4:** Link the conversation to the customer "Иван Петров". Check:
  - the card shows 1 order and 1 unpaid invoice;
  - "Create Sales Order" opens a prefilled SO, which you then discard without saving.
- [ ] **Step 5:** If the workspace has an agent in copilot/draft mode on livechat, approve one draft. Otherwise record "drafts: not exercised live (no draft-mode agent)" and rely on the Task 2 tests. Do not reconfigure agents.
- [ ] **Step 6:** Take screenshots at 1440 and 390 px. Leave `frontend` connected. Commit the results.
