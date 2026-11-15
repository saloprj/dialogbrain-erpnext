# Connect a channel from ERPNext — short design

Status: approved by Denys 2026-11-16 (v2, revised after an outside review).

Depends on DialogBrain `channels.connect_session.*`, which David is building and which is not on DialogBrain main yet. The contract items we need from him are listed at the end.

## Intent
An employee connects **their own** WhatsApp or Telegram number to the company's DialogBrain workspace without leaving ERPNext and without a DialogBrain login. In ERPNext the number then belongs to that employee: only they (and managers) see its chats. This is the flow UA-Pro asked for ("сразу привязали номер к менеджеру"), and we want it in our app so partners can sell it.

In DialogBrain itself the account belongs to the user who owns the site's API key, not to the employee. "Only the employee and managers see it" is an ERPNext rule. For employees who never log in to DialogBrain, that is the goal.

## Goals, non-goals, anti-goals
- Goal: a Chat Agent connects a number in under a minute. Methods: QR, WhatsApp pairing code, or Telegram phone number with code and 2FA password.
- Goal: the channel appears in ERPNext with `allowed_users` set to the owner, **even if the browser is closed right after the scan**.
- Goal: a manager can connect a number for a chosen employee, or as a shared number.
- Non-goal: Viber in this step. It is offered only when DialogBrain advertises it (see "Feature gates").
- Non-goal: disconnecting a number from ERPNext, and re-linking a number when an employee leaves. Managers edit `allowed_users` on DB Channel as today.
- Anti-goal: a personal number left visible to all agents, i.e. a new DB Channel created with empty `allowed_users` while its claim is pending.
- Anti-goal: one ERPNext user reading or answering another user's session.
- Anti-goal: phone numbers, codes or passwords in Error Log, the Webhook Log or any other log.
- Anti-goal: a duplicate channel when the number is already connected.
- Anti-goal: a number disconnected because someone closed the dialog after it connected.

## Approaches
- **B (chosen by Denys on 2026-11-16): each employee connects their own number, and it is assigned to them automatically.** Managers can connect for someone else. A (managers only, then assign) was rejected because it does not match how partners work.
- **Transport:** ERPNext proxies the four `channels.connect_session.*` tools through `DialogBrainClient.execute` with the site's key. The browser polls ERPNext and the key never leaves the server. Rejected: giving the browser a short-lived DialogBrain token, which needs a new token type and exposes more.
- **How the owner reaches the channel when the browser is gone** (the review's main point):
  - **Chosen:** a persistent **DB Channel Claim** row, resolved on the server, plus `external_ref` from DialogBrain when available.
  - Rejected: binding the owner only in the browser's `get` poll (the v1 draft). A closed tab or an early webhook leaves the channel open to every agent.
  - Rejected: relying on a channel sync. There is no periodic `channels.list` sync. Channels are created only at Connect/Re-sync and by the first webhook or backfill through `upsert_channel`.

## Design

### Data
New DocType **DB Channel Claim**. It is not visible to agents, and only System Manager can read it in the Desk. Fields:
- `session_id`
- `requested_by` (User)
- `owner_user` (User or empty for shared)
- `channel_type`
- `method`
- `status`: open | connected | expired | cancelled | failed
- `account_id` (once known)
- `expires_at` (DialogBrain's `expires_at`, refreshed on every `get`/`submit`)
- `resolved_at`

No phone, code or password is stored.

### Server, `dialogbrain/api/connect_channel.py`
All endpoints are whitelisted and POST only.
- **`start(channel, method, owner=None)`**
  - Role check: Chat Agent, Chat Manager or System Manager. An agent's owner is always themselves. A manager may choose any enabled user with Chat Agent, or none.
  - Rate limit with `frappe.rate_limiter.rate_limit` (user based): 10 starts per 10 minutes. "Start again" after `expired` counts as a start, and Telegram QR expires often.
  - Calls `channels.connect_session.start {channel, method, external_ref: <claim name>}`.
  - Inserts the claim. Returns the state.
  - WhatsApp pairing passes the phone through `submit`, not `start`, so the phone never appears in `start`'s parameters.
- **`get(claim)`, `submit(claim, kind, secret)`, `cancel(claim)`**
  - Allowed only for the claim's `requested_by`. Anyone else gets "not found".
  - `submit` has one parameter named `secret`, with `kind` one of phone, code or password. Frappe's Error Log masks parameter names containing "secret", so a crash cannot write the phone or code to the Error Log. Rate limit: 10 submits per claim.
  - `cancel` is sent to DialogBrain only while the claim is open and DialogBrain's status is a waiting one. After `connected` it only closes the dialog.
- **QR.** The contract is that `qr` is the raw QR string, which the server draws as inline SVG with `pyqrcode` (already a Frappe dependency, used for 2FA). If DialogBrain sends a `data:` URL, it is passed through as is.

### Resolving a claim
One function, `claims.resolve(claim, state)`, is called from three places:
1. `get`, when the browser polls and sees `connected`.
2. A sweep in the existing every-minute `reconcile.tick`. It calls `connect_session.get` for each open claim until `expires_at` plus a grace period, so a scan whose tab was closed still resolves within about a minute.
3. `upsert_channel`, when it is about to **create** a new DB Channel. If that `channel_type` has open claims:
   - It first tries `external_ref`, if DialogBrain supplied it with the account (in `channels.list` or the event).
   - Otherwise it calls `connect_session.get` on those claims (bounded: at most 3 calls, 5 s each) and binds the claim whose `account_id` matches.
   - If nothing matches while claims are still open, it **fails closed**: the channel is created with `allowed_users` set to the claim owners of that type, and managers see it as usual. The next sweep sets it right.

`resolve` does the following:
- Under a row lock on the claim (`SELECT … FOR UPDATE`), it calls `upsert_channel`.
- If the channel is new or has no allowed users, it sets `allowed_users` to the owner (empty owner means shared).
- If the channel already belongs to someone else, it leaves it and returns "already assigned to another employee", without naming them.
- It marks the claim connected.
- All writes use `ignore_permissions` or `frappe.db`, because Chat Agent has no write permission on DB Channel.
- It runs `agent_modes` for the channel and publishes a realtime update so the inbox filters refresh.

Idempotency: the same `account_id` always means the same DB Channel row.

### Feature gates
- The "Connect channel" button reads a stored copy of the gate (a new server-managed Settings field, `connect_channel_options`). It is refreshed at Connect/Re-sync and **also in `agent_modes.hourly`**, so the button appears within an hour of David's deploy without an admin clicking Re-sync. When the tool is missing, the button is hidden.
- Channels and methods come from an optional tool, `channels.connect_session.options`, which returns `{channels: {whatsapp: [qr, pairing_code], telegram: [qr, phone], …}}`. The tool catalog carries no input schemas, so it cannot tell us this. The answer is stored with the gate and refreshed hourly.
  - Viber is offered only if `options` lists it.
  - WhatsApp pairing code is offered only if `options` lists it. It is new work for David: DialogBrain has no `requestPairingCode` today.
  - If the `options` tool is absent, only QR for WhatsApp and Telegram is offered.

### Dialog
It is a Frappe dialog with no new JS dependencies. It is opened from the Chats page menu and from the DB Channel list.
1. Pick the channel.
2. Managers only: pick the owner, an employee or "shared".
3. Pick the method.
4. Show the live state.
   - QR or pairing code, plus a countdown to `expires_at`.
   - Polling every **3 s**, paused while the tab is hidden, with backoff after errors.
   - "Start again" after `expired`.
   - Clear messages for `billing_required` and for a key without edit rights. The latter is our own text: "the DialogBrain key needs edit rights in the workspace to connect channels", not the agent-mode wording from `api/channels.py`.

Each poll holds a gunicorn worker for the round trip to DialogBrain (client timeout 20 s). That is fine for a few people connecting at once, and the 3 s interval plus the hidden-tab pause keep it bounded.

### Translations
New strings in ru and uk.

## Testing
Unit tests use a fake `DialogBrainClient`.
- Every status:
  - waiting_scan, need_code, need_password and need_pairing_code;
  - connected, expired, error and billing_required.
- Ownership:
  - agent vs. manager;
  - another user's claim gets "not found";
  - "already assigned" names nobody.
- **The closed-tab path:**
  - The sweep resolves the claim.
  - A webhook arriving first for an unknown account is matched through the claim, or fails closed and is never open to all agents.
- Rate limits.
- No phone, code or password in Error Log, including for an exception raised inside `submit`.
- Cancel after connected sends nothing to DialogBrain.
- Feature gates: no tool means no button, and `options` controls Viber and pairing.

Once David's branch is on dev, a live e2e check against dev DialogBrain. Until then the feature stays hidden by the gate, so it can ship to prod safely.

## Contract items to confirm with David
1. `start` accepts `external_ref`, stores it with the session and the resulting account, and returns it in `get`, `channels.list` and the `channel.connected` event.
2. `get` keeps answering `connected` with `account_id` for at least 10 minutes after the connection, so the sweep can catch it.
3. `cancel` on a connected session does nothing. It never disconnects.
4. `qr` is the raw QR string.
5. A tool `channels.connect_session.options` returns the supported channels and, for each, its methods (qr, pairing_code, phone).
6. Rejections use clear error types: `billing_required`, `permission_denied` (key lacks EDIT), `unsupported_channel`, and `rate_limited`.

## Left to the builder
Dialog copy and layout, the exact grace period, backoff numbers, and the claim retention period.
