# DialogBrain for ERPNext — omnichannel inbox — design

Date: 2026-11-13
Status: draft for approval

## 1. Intent

DialogBrain is a multi-channel messaging platform with AI agents (Telegram, Viber,
WhatsApp, Instagram, Messenger, email, livechat, …). Many SMB customers run their
business in ERPNext. This project is a **product**: an installable Frappe app that
any DialogBrain customer with ERPNext installs themselves, pastes a DialogBrain API
key, and gets a full omnichannel inbox inside ERPNext Desk.

Reference UX: UA-CRM inbox (`/desk/ua-crm/ua-crm-inbox`) — three panes: chat list
with filters / conversation with composer / customer card with orders and
"create order" / "link customer" actions.

**Who uses it:** sales/support managers who **live only in ERPNext and never open
DialogBrain**. The business owner opens DialogBrain once to connect channels,
configure the AI agent and pay. So the ERPNext inbox must be a complete daily
workplace, not a companion view.

**AI agent role: both modes.**
- *Copilot:* agent prepares a draft reply using ERPNext data (price, stock, order
  status); the manager edits and sends.
- *Autopilot with handoff:* agent answers routine questions itself and hands the
  conversation to a human when needed (purchase intent, complaint, unknown).
- Switchable per channel (default) and overridable per conversation ("take over" /
  "give back to agent").

## 2. Goals

1. Managers see all DialogBrain conversations of the workspace in ERPNext in near
   real time (target: new inbound message visible < 3 s after DialogBrain receives
   it, when webhooks are reachable).
2. Read & reply: text, images, files, voice (playback), quick-reply templates.
3. Work organisation: assign to self/colleague, open/closed status, filters
   (open/closed, mine, unassigned, unanswered, channel, manager), search by
   name/phone/text, unread counters, desk notification + sound.
4. CRM link: each conversation is linked to an ERPNext Customer or Lead (auto by
   phone/email, otherwise manual "link customer" / "create lead"). Right pane shows
   customer, contact data, order count/total, recent Sales Orders and unpaid Sales
   Invoices; buttons "Create Sales Order", "Create Quotation", "Create Lead".
5. AI: show agent drafts in the composer (approve / edit+send / reject); show
   autopilot replies inline (labelled "AI"); handoff requests highlight the chat and
   notify; per-channel mode switch and per-conversation take-over.
6. Agent uses ERPNext data: on connect, the app registers ERPNext as an integration
   in DialogBrain with a small set of safe tools (see §7).
7. Reports: first-response time, unanswered conversations, conversations per
   manager/channel.
8. Self-service install: `bench get-app` + `bench install-app`, a settings page with
   "Connect" — no manual work in DialogBrain or ERPNext beyond pasting the API key.
9. Supports ERPNext/Frappe **v15 and v16**. UI translations: uk, ru, en.

## 3. Non-goals (v1)

- Connecting/configuring channels, agent prompts, billing — stays in DialogBrain.
- Payment links, delivery (Nova Poshta TTN), fiscal receipts, mass mailings,
  funnels/dashboards like UA-CRM tabs — later modules.
- Voice calls, group chats moderation.
- Frappe Marketplace listing (v1 = public GitHub repo; marketplace later).

## 4. Anti-goals (failure even if it "works")

- Lost, duplicated or out-of-order messages; a manager reply sent twice.
- The agent revealing one customer's data (orders, invoices) to another person.
- Slowing down ERPNext (heavy sync in web workers, unbounded tables, N+1 calls).
- Managers needing to open DialogBrain for daily work.
- Secrets (API keys, webhook secret) stored or logged in clear text.

## 5. Constraints (facts about DialogBrain today)

Base: DialogBrain api-gateway. Auth: API key `db_live_…` via `Authorization:
Bearer` or `X-API-Key`; one key = one workspace.

Usable with an API key:
- `POST /api/tools/{tool_id}/execute` body `{params, mode, skip_approval,
  thread_id}` — runs any MCP tool. Relevant tools:
  - `search.threads` (only_unanswered, channel, only_unread, since,
    include_last_messages), `threads.list`
  - `messages.read_history` (paging, before, direction)
  - `messages.send` (`thread_id`, `text`, `attachments`=[workspace file ids],
    `reply_to_message_id`)
  - `files.create_upload_url` / `files.complete_upload` / `files.upload`
  - `contacts.find`, `contacts.profile`, `contacts.update`
  - `agents.list_drafts`, `agents.approve_draft` (draft_id, edited_text),
    `agents.reject_draft`, `agent.silence` (stop agent on a thread),
    `agents.list`, `agents.trigger_update` (send_mode auto/draft)
  - `integrations.add_endpoints`, `integrations.set_auth`,
    `agents.set_integration`
- Outgoing webhooks: `POST /api/v1/webhooks {url, event_types[]}` → secret
  `whsec_…` returned once. Events used: `message.received` (fires for inbound
  **and** outgoing — use `data.message.is_outgoing`), `message.updated`,
  `message.deleted`, `conversation.started`, `conversation.updated`,
  `draft.created`, `handoff.requested`, `contact.updated`. Payload `{event, data,
  delivery_id}`; message data has `channel, account_id, thread_id, message{…,
  attachments[{file_id, filename, mime_type, download_url, media_type}]}, thread`.
  Signature `X-DialogBrain-Signature: sha256=<hex>` = HMAC-SHA256(raw body, key =
  hex-decoded part after `whsec_`). Retries 5/10/20/40/80 s then subscription
  auto-pauses (`PATCH /api/v1/webhooks/{id}/activate` to resume).

Verified details (DialogBrain code, plan-1 review 2026-11-13):
- **Message identity:** in events `message.message_id` is the *channel* ref
  (e.g. Telegram number / `whatsapp:3EB0…`) and `message.db_id` is DialogBrain's
  global id. The `messages.send` path publishes `message_id=str(db id)` first and
  channel sync may later deliver the same message with the channel ref. → The
  mirror's unique key is **`db_id`**; `message_id` (channel ref) is stored
  non-unique.
- **Text:** event `text` is truncated to 100 chars; the full body is in `html`.
  → Always store `html` as the message text.
- **Thread id:** event `thread_id` is the thread's channel ref (fallback: internal
  id); `thread.db_id` is the internal id. Which form `messages.send` /
  `messages.read_history` accept is verified when fixtures are recorded.
- **Authorship:** sends through the tool path publish `from_id="system"`,
  `from_name=<agent name>` for agent sends or `"You"` otherwise. Outgoing messages
  can also come from the DialogBrain UI or the owner's phone (not via ERPNext).
- **Listing/reading:** `search.threads.since` is a **date** (`YYYY-MM-DD`), not a
  timestamp; `include_last_messages` ≤ 2 and truncated. `messages.read_history`
  pages stored chats with `limit`/`offset` (`next_offset`), filters `date_from`/
  `date_to` (days, UTC); its `direction` means incoming/outgoing, not paging
  direction. There is no "messages after X" cursor.
- **Webhook pausing:** a subscription is deactivated after **3 consecutive failed
  deliveries** → the receiver must answer 2xx for anything that is not a real
  authentication failure.
- **Agent pause per thread:** `agent.silence` only ends the agent's current turn —
  it is NOT a per-thread pause. Per-thread pause exists as `POST
  /threads/{id}/pause-agent` / `unpause-agent` (user login only today). A human
  reply auto-pauses the agent and cancels obsolete drafts (`human_replied`) only
  when the tool context carries the caller's `api_key_id`, which
  `/api/tools/{id}/execute` currently does not set.
- **Idempotency:** `messages.send` is idempotent per `request_id`, but
  `/api/tools/{id}/execute` does not pass one through.

Gaps (handled on ERPNext side in v1):
- No thread status (open/closed) and no assignment via API key → stored in ERPNext.
- `POST /api/v1/messages/send` is broken (wrong param name) → use tools/execute.
- No external-id on contacts → mapping stored in ERPNext.
- No embeddable inbox → UI is built natively in ERPNext.

DialogBrain-side changes that are **part of v1** (one PR series in the dialogbrain
repo, Plan 3):
1. Signed thread context for integration tool calls (§7.3).
2. `/api/tools/{id}/execute`: accept `request_id` (→ ToolContext, enables send
   idempotency) and set `api_key_id` on the context (→ a manager reply from ERPNext
   counts as a human reply: agent auto-pauses, obsolete drafts are cancelled).
3. `pause-agent` / `unpause-agent` per thread callable with an API key (REST or a
   tool).
Nice-to-have later: a cursor/changes API for messages (would make reconcile
cheaper); fixing `/api/v1/messages/send`.

## 6. Approaches considered

| | Approach | Trade-offs |
|---|---|---|
| **A (chosen)** | **Mirror** conversations/messages into ERPNext DocTypes, fed by signed webhooks + periodic reconciliation; send via DialogBrain API; media referenced, not copied | Fast UI, ERPNext search/reports/permissions, durable links to documents (same model as UA-CRM). Cost: sync logic (backfill, dedupe, reconcile), needs public ERPNext URL for webhooks (fallback: polling). |
| B | Live proxy: no stored messages, every view calls DialogBrain | Simple, but slow, filters/search limited to DialogBrain API, no reports, inbox empty when DialogBrain is down. |
| C | Embed DialogBrain inbox via iframe + ERPNext panel inside DialogBrain | Full features instantly, but needs DialogBrain changes (frame headers, SSO), foreign-looking UI, no ERPNext permissions/reports. |

Picked A because managers live only in ERPNext: speed, search, reports and document
links require local history.

## 7. Design

### 7.1 App & data model

Frappe app `dialogbrain` (repo `dialogbrain_erpnext`), module "DialogBrain".

DocTypes (prefix `DB`):

- **DialogBrain Settings** (Single): base_url, api_key (Password), workspace_id/
  name (read-only after connect), webhook_id, webhook_secret (Password),
  webhook_status, public_site_url (defaults to site URL), sync_mode
  (webhook | polling), last_reconcile_at, backfill_days (default 30),
  default_company, erpnext_agent_user, integration_id; buttons Connect / Disconnect
  / Re-sync / Test.
- **DB Channel**: one per DialogBrain channel account (account_id, channel type,
  title), company, default_manager (User), agent_mode (copilot | autopilot | off),
  enabled, **allowed_users** (child table of Users; empty = every Chat Agent may
  see this channel — so small companies need no setup).
- **DB Conversation**: thread_id (unique), channel (Link DB Channel), title,
  contact_name, phone, email, handle, avatar_url, db_contact_id,
  party_type (Customer | Lead), party (Dynamic Link), contact (Link Contact),
  link_source (auto_verified | auto_unverified | manual),
  status (Open | Closed), assigned_to (User), agent_override (default | human |
  agent), needs_human (Check), last_message_at, last_message_preview,
  last_inbound_at, first_response_seconds (per opening), unread_count, opened_at,
  closed_at.
  Indexes: (status, last_message_at), (assigned_to, status), phone, email.
- **DB Message**: db_id (unique — DialogBrain global id; empty only while a reply
  is pending), message_id (channel ref, not unique), conversation (Link), direction (in | out),
  author_type (customer | manager | agent | system), sent_by (User, for replies from
  ERPNext), text, sent_at, attachments (child table DB Message Attachment:
  file_id, filename, mime_type, media_type, size — no URLs stored; links are
  resolved on demand by file_id), delivery_status (pending | sent | failed |
  unknown | deleted), client_ref (idempotency key, unique, NULL for inbound),
  raw (JSON, trimmed). Text = event `html` (full body).
  Index (conversation, sent_at).
- **DB Draft**: draft_id, conversation, text, created_at, status (pending |
  approved | rejected | superseded).
- **DB Quick Reply**: title, shortcut, text (supports `{customer_name}` etc.),
  company (optional), owner/shared.
- **DB Note**: conversation, text (internal notes; never sent).
- **DB Webhook Log**: delivery_id (unique), event, payload, received_at, status
  (queued | processed | failed), attempts, error — for dedupe, retry and support;
  rows (they contain customer text) are deleted after 14 days by a daily job.

Roles: **Chat Agent** (sees **all conversations of channels they are allowed on**
— DB Channel.allowed_users empty or containing them; reply, assign to self or
take an unassigned one, close; enforced server-side via `permission_query_conditions`
+ `has_permission` on DB Conversation/DB Message), **Chat Manager** (all conversations,
assign anyone, settings for channels, reports). System Manager configures Settings.

### 7.2 Sync

- **Connect** (Settings → Connect): capability check (tools/execute works;
  required tools exist in the tool list: `search.threads`, `messages.read_history`,
  `messages.send`, `files.create_upload_url`, `channels.list`; webhooks API
  responds; fail with a message naming what is missing); validate key (`agents.list` or
  `workspace.current`), store workspace; create webhook subscription to
  `{public_site_url}/api/method/dialogbrain.api.webhook.receive` for events in §5;
  store secret; fetch channels into DB Channel; enqueue backfill; register ERPNext
  integration (§7.3).
- **Two network modes (user decision: support both).**
  - *Public* (site reachable from DialogBrain): webhooks + agent tools.
    DialogBrain has no "send test event", so Settings shows webhook health from
    `GET /api/v1/webhooks/{id}/deliveries` (last delivery time/status, active
    flag) and warns when deliveries fail or the subscription is paused.
  - *Private* (behind NAT / LAN): sync_mode=polling (reconcile every 1 min, plus
    an on-demand refresh when a user opens the inbox / a conversation), agent tools
    not registered. Settings shows a persistent warning "AI agent can't see ERPNext
    data — make the site public or use a tunnel" with a docs link (cloudflared /
    frp). Switching modes later is a button, not a reinstall.
- **Webhook endpoint** (`allow_guest`, POST only): verify HMAC on raw body (bad
  signature → 401, the only non-2xx besides 413 for > 1 MB), insert DB Webhook Log
  by delivery_id (duplicate → 200 no-op), `frappe.enqueue` processing on `short`
  queue, return 200 fast. Not connected → 200 and drop (so DialogBrain doesn't pause
  the subscription). Failed or stuck (`queued` > 10 min) log rows are re-processed
  by the scheduler tick (max 5 attempts).
- **Processing**: upsert conversation by thread_id, upsert message by **db_id**
  (dedupe; `message.received` and `message.updated` go through the same upsert +
  echo matching, because an outgoing message may come back as an update), match
  the echo of an ERPNext-sent reply by db_id returned by send, falling back to
  same conversation + outgoing + identical full text within 120 s; update
  counters/preview, auto-link party (§7.4), reopen closed conversation on new
  inbound, `frappe.publish_realtime` to permitted users (every message).
  **Notifications** (Notification Log + sound): for an assigned conversation — the
  assignee on every inbound; for an unassigned one — permitted users only on the
  first inbound of a new or re-opened conversation (no spam with many agents);
  handoff requests always. Reconcile in polling mode notifies the same way;
  only backfill is silent.
  **Authorship:** out + matched to an ERPNext reply → manager + sent_by; out with
  `from_id == "system"` and `from_name` not in ("You", "") → agent; other out →
  manager with empty sent_by (sent from DialogBrain UI or a phone). `draft.created` → DB Draft; `handoff.requested` → needs_human=1,
  notify; `message.updated/deleted` → update/soft-delete.
- **Backfill** (long queue): `search.threads since=now-backfill_days`, then
  `messages.read_history` per thread, paged; resumable; progress in Settings.
- **Reconcile** (scheduler every 5 min in webhook mode, every 1 min in polling
  mode), on today's API: `search.threads since=<date of (last_reconcile_at − 1
  day)>` paged by offset; for each thread whose last-activity timestamp is newer
  than our newest stored `sent_at` for it, `messages.read_history thread_id,
  date_from=<day of our newest message>` paged by offset, upsert by db_id
  (duplicates are no-ops). `last_reconcile_at` advances only after a fully
  successful pass, so an outage of any length — including a paused subscription,
  which drops events — is recovered (the `since` date simply goes further back).
  Also re-activates a paused subscription.
  *Load estimate:* a shop with 200 active threads/day → ~4 `search.threads` pages
  plus one read per changed thread per pass; at 1-min polling ≈ 5–20 calls/min.
  Acceptable for v1; a cursor API in DialogBrain is a later optimisation.
- **Ordering**: messages ordered by DialogBrain `sent_at`; UI sorts, never trusts
  arrival order.

### 7.3 Agent ↔ ERPNext tools

On Connect the app creates ERPNext user `dialogbrain-agent@<site>` (roles: a custom
minimal role, no desk), API key/secret, and calls
`integrations.add_endpoints` (auth custom header `Authorization: token k:s`) with
whitelisted app methods (not raw `/api/resource`):

- `dialogbrain.agent_api.find_customer(phone|email)` → name, type, outstanding
- `dialogbrain.agent_api.get_item(query)` → items with price (default selling price
  list), stock qty (sum of Bin.actual_qty over allowed warehouses), in_stock
- `dialogbrain.agent_api.order_status(phone|email, order_no?)` → that customer's
  recent Sales Orders with status, delivery, per_billed
- `dialogbrain.agent_api.unpaid_invoices(phone|email)`
- `dialogbrain.agent_api.create_lead(name, phone, email, note)`
- `dialogbrain.agent_api.create_draft_order(phone|email, items[{item_code, qty}])`
  → Draft Sales Order, never submitted; notifies assigned manager.

Write tools are disabled for the agent by default (DialogBrain auto-denies writes);
the owner enables them.

**Data-leak protection — in v1 scope (user decision).** Customer-scoped tools
(`find_customer`, `order_status`, `unpaid_invoices`, `create_draft_order`) take
**no phone/email/customer argument from the LLM**. Instead:

- *DialogBrain change (PR in the dialogbrain repo, part of v1):* when an agent run
  that belongs to a thread calls an HTTP integration tool, DialogBrain adds headers
  `X-DialogBrain-Thread-Id`, `X-DialogBrain-Timestamp` and
  `X-DialogBrain-Context-Signature = HMAC-SHA256(secret, "{timestamp}.{thread_id}.{method}.{path}")`.
  The secret is per integration, generated at Connect and stored on both sides
  (ERPNext: Password field). Integration config gets a flag `send_thread_context`
  so other integrations are unaffected. Calls not made from a thread run carry no
  context.
- *ERPNext side:* verify signature and timestamp (±5 min); look up DB Conversation
  by thread_id → its linked party, **only if `link_source` is `manual` or
  `auto_verified`** (phone verified by the channel itself: WhatsApp, Viber,
  Telegram with shared phone). Links made from a phone/email typed in livechat or
  from email are `auto_unverified` and give the agent no customer data. No context, bad signature or unlinked
  conversation → the tool returns "customer not identified" (never data). Generic
  tools (`get_item`, `create_lead`) work without context.
- Every call is logged (DB Agent Call Log: tool, thread, party, result size).

If that DialogBrain PR is not yet deployed on a workspace (capability check at
Connect), customer-scoped tools are not registered — the agent still sees catalogue,
prices and stock.

### 7.4 Party linking

On new conversation / contact.updated: normalise phone (digits; leading `00`
stripped) and email; look up Contact by phone/mobile/email → its Customer
(preferred) or Lead link. Phone equality: full digit equality; a last-9-digits
match is allowed only when one side is in local format (starts with a single `0`),
to avoid cross-country false matches. `link_source` = auto_verified when the
matched phone comes from a channel that verifies it, otherwise auto_unverified;
manual links = manual.
Exactly one match → link automatically; several → leave unlinked and show
candidates in the card; none → unlinked with "Create Lead" / "Link customer".
Manual link also creates/updates ERPNext Contact phone/email. Creating Sales Order /
Quotation from the card opens a new form prefilled with party, company of the
channel; saved document gets a link back to the conversation (custom field
`db_conversation` on Sales Order, Quotation, Lead).

### 7.5 Sending

Composer → `dialogbrain.api.inbox.send(conversation, text, files[], client_ref)`:
insert DB Message (pending, client_ref unique → double click / retry safe),
enqueue job (sends client_ref as `request_id` once DialogBrain accepts it — Plan
3; until then a timed-out send is NOT retried automatically but marked "unknown"
and resolved by reconcile/echo matching; the job stores `db_message_id` from the
response as db_id):
upload files (`files.create_upload_url` → PUT → `complete_upload`),
call `messages.send`, store returned message_id, status sent / failed (+ retry
button). First reply on an opened conversation sets first_response_seconds. Sending
as manager auto-assigns an unassigned conversation to them; once DialogBrain sets
`api_key_id` on tools/execute (Plan 3) the reply also auto-pauses the agent on that
thread and cancels obsolete drafts (= take over).

Drafts: Approve → `agents.approve_draft(draft_id, edited_text)`; Reject →
`agents.reject_draft`. Media: thumbnails/files served via
`dialogbrain.api.inbox.media(file_id)` which proxies `download_url` with auth and
permission check (no copies stored; short private cache allowed). `download_url`
may be presigned/expiring → on 403/404 re-resolve by file_id via `files.info` /
`files.read` tools. Size limit for proxying: builder's call (stream, don't buffer).

### 7.6 Agent modes

- DB Channel.agent_mode maps to the DialogBrain agent trigger for that channel:
  copilot = send_mode draft, autopilot = send_mode auto, off = trigger disabled.
  Changing it in ERPNext calls `agents.trigger_update`. If the workspace has several
  agents/triggers per channel, Settings shows the mapping and the user picks the
  agent.
- Per conversation: "Take over" → DialogBrain `pause-agent` for the thread +
  agent_override=human; "Give back to agent" → `unpause-agent` + override=agent
  (both need the API-key access added in Plan 3).

### 7.7 UI

Desk page `/app/dialogbrain-inbox` ("Chats"), Vue 3 bundle built with Frappe's
esbuild (`*.bundle.js`), works in v15 and v16 desk. Layout:

```
┌ Filters: [Open▾] [All managers▾] [search…]           ┐┌ Header: name · channel · phone · [Open|Close] [Assign▾] [Take over]┐┌ Contact / Customer card ┐
│ [All][TG][Viber][IG][FB][Web] chips   Unanswered(7) Mine││ messages (in left, out right, AI badge, attachments, voice player)  ││ name, phone, channel     │
│ ─────────────────────────────────────────────── ││ …                                                                   ││ manager, opened, 1st resp│
│ ● Name  [manager]  preview…          13:30  ③      ││ [AI draft: "…"  Approve | Edit | Reject]                            ││ Notes (+ add)            │
│ ...                                              ││ [⚡quick replies][📎][😊] [text…………………] [Send]                     ││ Customer: orders N, sum  │
└──────────────────────────────────────────────────┘└─────────────────────────────────────────────────────────────────────┘│ Orders list, unpaid inv. │
                                                                                                                         │ [Create order][Link…]    │
                                                                                                                         └──────────────────────────┘
```

Realtime via `frappe.realtime` events `dialogbrain:conversation` /
`dialogbrain:message`; list virtualised/paged (50); unread badge in navbar;
mobile-width usable (single pane with back navigation).
Also: "Chats" tab/section on Customer and Lead forms listing linked conversations
with a link into the inbox.

### 7.8 Reports

Script reports: First Response Time (by manager/channel/day), Unanswered
Conversations, Conversations by Channel/Manager. Workspace "DialogBrain" with
shortcuts.

### 7.9 Security

API key, webhook secret, agent user secret in Password fields; never logged.
Webhook: HMAC + delivery_id dedupe + body size limit. All inbox API methods check
role and conversation visibility server-side. Agent API methods only for the agent
user.

### 7.10 Quality

- Unit tests (FrappeTestCase) with a mocked DialogBrain client: webhook signature,
  dedupe, ordering, party matching, send idempotency, permissions, agent API
  scoping.
- Integration test on the local stack: ERPNext (frappe_docker, this machine) ↔
  dialogbrain-dev (local api-gateway, reachable via host.docker.internal).
- CI: GitHub Actions matrix v15 / v16.

## 8. Decided by the user

- Product for any DialogBrain customer with ERPNext (self-install).
- Managers live only in ERPNext → full inbox.
- AI agent: both modes (copilot + autopilot with handoff).
- Approach A (mirror).
- Both network modes: public (webhooks + agent tools) and private (polling, agent
  without ERPNext data, with a visible warning).
- Signed thread context in DialogBrain is part of v1; customer-scoped agent tools
  never trust LLM-supplied identifiers.
- Reconcile on today's API (date + offset), cursor API later (Claude's call after
  plan review; load estimate in §7.2).
- Channel-level access for managers, default "all channels for everyone".

## 9. Builder's calls (proposed by Claude, open to correction)

- Status/assignment kept in ERPNext in v1; first-class support in DialogBrain later.
- Media proxied, not copied.
- Agent tools via whitelisted app methods, write tools off by default.
- Roles Chat Agent / Chat Manager; translations uk/ru/en; v15+v16.
- Distribution: public GitHub repo first.
- Exact Vue component structure, report SQL, polling intervals tuning.
