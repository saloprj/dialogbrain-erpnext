# Connect a channel from ERPNext — short design

Status: draft 2026-11-16, waiting for Denys's approval. Depends on DialogBrain `channels.connect_session.*`, which David is building and which is not on DialogBrain main yet.

## Intent
An employee connects **their own** WhatsApp or Telegram number to the company's DialogBrain workspace without leaving ERPNext and without a DialogBrain login. The number then belongs to that employee: only they (and managers) see its chats. This is the flow UA-Pro asked for in their CRM ("сразу привязали номер к менеджеру"). We want it in our app so partners can sell it.

## Goals, non-goals, anti-goals
- Goal: a Chat Agent connects a number in under a minute, using a QR scan, a WhatsApp pairing code, or a Telegram phone number with code and 2FA password.
- Goal: the new channel appears in ERPNext right away (a DB Channel row) with `allowed_users` = that employee.
- Goal: a manager can connect a number on behalf of a chosen employee, or as a shared number.
- Non-goal: Viber in this step. The screen offers it only when DialogBrain accepts `channel: viber`. David builds Viber as a second stage.
- Non-goal: disconnecting a number from ERPNext, or re-linking it when an employee leaves. Managers already edit `allowed_users` on DB Channel; disconnecting stays in DialogBrain for now.
- Anti-goal: one ERPNext user polling or answering another user's session (a QR or code gives access to someone's messenger).
- Anti-goal: phone numbers, codes or passwords in logs, error logs or the Webhook Log.
- Anti-goal: a duplicate channel when the number is already connected.

## Approaches
- **B (chosen by Denys on 2026-11-16): each employee connects their own number, and it is assigned to them automatically.** Managers can also connect for someone else.
- A (rejected): only managers connect numbers and then assign them. Simpler, but it doesn't match how partners like UA-Pro work.
- How we talk to DialogBrain:
  - **Chosen:** ERPNext proxies the four `channels.connect_session.*` tools through the existing `DialogBrainClient.execute` with the site's workspace key. The browser polls ERPNext every 2 s, and ERPNext calls `get`. Nothing new is exposed to the browser, and the key never leaves the server.
  - Rejected: give the browser a short-lived DialogBrain token and poll DialogBrain directly. That needs a new token type in DialogBrain and exposes more.

## Design
- **Entry points:** a "Connect channel" button in the Chats page menu and on the DB Channel list. It is visible to Chat Agent, Chat Manager and System Manager, and only while the workspace's tool catalog lists `channels.connect_session.start`. Otherwise the button explains that the DialogBrain server does not support it yet.
- **Dialog (Frappe dialog, no new JS dependencies):**
  1. Pick a channel: WhatsApp or Telegram. Viber appears only if supported.
  2. Managers only: pick the owner, either an employee (Chat Agent) or "shared (no owner)".
  3. Pick a method:
     - WhatsApp: QR (default) or "pairing code by phone number". That asks for the phone and shows the 8-character code to enter on the phone.
     - Telegram: QR (default) or phone number, then the code, then the 2FA password if `need_password`.
  4. Show the live state:
     - The QR comes from the current `get` and is redrawn whenever it changes.
     - A countdown runs to `expires_at`. On `expired` there is a "Start again" button.
     - Errors show `error` as text, including `billing_required` with a clear message.
     - Closing the dialog calls `cancel`.
- **Server (`dialogbrain/api/connect_channel.py`, whitelisted, POST):**
  - `start(channel, method, owner=None)`:
    - Checks the role and the owner: an agent's owner is always themselves; a manager may pick any enabled user with Chat Agent, or none.
    - Rate limit: 5 starts per user per 10 minutes.
    - Calls `channels.connect_session.start`.
    - Stores `session_id → {user, owner, channel}` in the Frappe cache with a TTL of about 5 minutes. Returns the state.
  - `get(session_id)` / `submit(session_id, phone|code|password)` / `cancel(session_id)`:
    - Allowed only for the user who started the session; otherwise "not found".
    - `submit` is rate-limited (10 per session). Its values are never logged and never stored.
  - **QR:** drawn on the server as an inline SVG with `pyqrcode`, which Frappe already ships for 2FA.
  - **On `connected` (in `get`):**
    - Call `upsert_channel(account_id, channel, name or phone)`.
    - If the channel is new, or exists with no allowed users, set `allowed_users` to the owner. If it already belongs to someone else, leave it and tell the user who owns it (idempotent: same `account_id` means the same row).
    - Run `agent_modes` for that channel so its agent mode shows at once.
    - Publish a realtime update so the channel filter and the inbox refresh.
  - **Errors:** DialogBrain errors are passed on as text with the key scrubbed (the same `_reason` pattern as `api/channels.py`). Nothing goes to Error Log with request params.
- **Webhooks:** `channel.connected` / `needs_reauth` are optional for us. Polling `get` is enough for the dialog, and the existing hourly channel sync still picks up channels connected elsewhere. If DialogBrain sends `needs_reauth`, the DB Channel shows a "reconnect needed" status line. That status line is a small follow-up, not part of this step.
- **Translations:** new strings in ru and uk, as for the rest of the app.

## Testing
- Unit tests with a fake `DialogBrainClient`:
  - Every status path: waiting_scan, need_code, need_password, need_pairing_code, connected, expired, error/billing_required.
  - Ownership rules (agent vs. manager, someone else's session).
  - Rate limits.
  - No secrets in logs.
  - The connected → DB Channel + `allowed_users` result, and idempotency.
- Live e2e against dev DialogBrain once David's branch is on dev. Until then the feature is hidden by the catalog check, so it can ship to prod safely.

## Left to the builder
Exact cache keys, dialog copy and layout, polling backoff after errors, and how the countdown is drawn.
