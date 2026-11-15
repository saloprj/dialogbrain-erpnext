# DialogBrain recorded fixtures

Recorded 2026-11-13 against the local dialogbrain-dev api-gateway (workspace 128), API key
workspace-locked. Personal data is replaced with fake values; structure and types are kept.
Webhook bodies are exactly what the receiver got (minus the `X-DialogBrain-Signature` header).

All `$.` paths below are relative to the fixture root. Tool results are wrapped as
`{"success", "data", "error", "effects_applied", "metadata", "timestamp"}`; the client returns `data`.

## Provenance

| Fixture | Source |
|---|---|
| `workspace_current`, `channels_list` (subset of 28 accounts, sanitized), `tools_catalog_trimmed` (7 of 329 tools), `tools_catalog_ids` | live |
| `search_threads` | live; telegram DM + group threads re-written with fake values, the livechat thread is the real fake test thread |
| `read_history_page1/page2/full`, `messages_send`, `messages_send_by_ref`, `messages_edit_livechat` | live, on a fresh livechat test thread (fake data) |
| `webhook_message_received_inbound` (Telegram DM), `webhook_message_received_group` | live capture, sanitized (names, phone, username, text, ids) |
| `webhook_message_received_outgoing`, `webhook_message_received_outgoing_by_ref` | live capture of the `messages.send` echo (livechat, fake data) |
| `webhook_message_deleted` | live capture, sanitized |
| `webhook_message_updated_derived` | **from code, not captured** (see below) |
| `webhooks_create/list/deliveries` | live (secret replaced by a dummy) |

## Thread id: which form is accepted where

- Search result, `$.data.threads[i]`: `id` == `thread_id` (string of the internal id, e.g. `"8451249"`), plus `channel_ref` (e.g. `"livechat:<uuid>:<visitor>"`, `"telegram:324622388"`).
- **`messages.send` accepts both** the internal id (`"8451249"`) and the channel ref (`livechat:...`) in `params.thread_id` (both verified live, both land on the same thread). Response: `$.data.thread_id` is the internal **int** (`8451249`), `$.data.channel_ref` the ref.
- **`messages.read_history` accepts both** too (verified; identical result). Response `$.data.thread.id` is the internal int.
- Webhook `$.data.thread_id` is the **channel ref**; `$.data.thread.db_id` is the internal id (string). BUT the `thread` sub-object is inconsistent across sources: for `source == "channel_sync_service"` it has `thread_id` (ref) + `db_id`; for `source == "messages.send"` it has NO `db_id` and `$.data.thread.thread_id` is `"livechat:8451249"` (channel + internal id, not the ref). Always use `$.data.thread_id` (ref) as the key, and `$.data.thread.db_id` only when present.
- `message.deleted`: `$.data.thread_id` is **double-prefixed** (`"telegram:telegram:-100..."`) - do not match it to a thread by equality; strip a duplicated channel prefix (or look up the message by `message_ids`).

## Channel, account, contact

- Channel type: `$.data.channel` (webhook), `$.data.threads[i].channel` (search). Account id: `$.data.account_id` (webhook, string), `$.data.threads[i].channel_account_id` (string) and `.account.id` (int). Account: `.account{id, channel, name, phone, email, workspace_id}`.
- Thread kind: `threads[i].kind` in `dm | group | livechat`; webhook `$.data.thread.kind` (channel_sync events only).
- Telegram DM contact (inbound webhook): name `$.data.message.from_name` / `.sender.name` (also `.metadata.sender_name`); handle `.metadata.username`; phone `.metadata.phone` (digits, no `+`); id `.from_id` / `.sender.id` (== `.peer` for DMs). **No email** in Telegram events. Phone/username can be absent (privacy; the group fixture has neither). Telegram phone numbers are registration-verified, but the field is only present when the user exposes it; the payload carries no "verified" flag. Treat phone as unverified for matching unless you decide otherwise per channel.
- Group inbound: `.peer` is the group ref, `.from_id`/`.from_name` the author; thread title is `$.data.thread.title`.
- Livechat: `.peer` = `<widget-uuid>:<visitor>`; visitor name only in `threads[i].title` / `read_history.messages[].sender.name`.

## Message fields (webhook `$.data.message`)

- `db_id` (string, DialogBrain global id -> **unique key**), `message_id` (channel ref, e.g. `telegram:9054119`; for `messages.send` echoes it is `livechat:msg_...`, the channel ref of the sent message), `html` (**full body**), `text` (truncated to 100 chars), `sent_at` (ISO8601 with tz; inbound `+00:00` seconds precision), `is_outgoing` (bool), `from_id`, `from_name`, `sender{id,name}`, `media_type` (`text`, ...), `attachment_file_ids` (list), `attachments` (list), `metadata` (channel specific; null for livechat), `is_system_event`.
- Outgoing echo of `messages.send` (`source == "messages.send"`): `is_outgoing: true`, `from_id: "system"`, `from_name: "You"` (agent sends: agent name), `attachments`/`attachment_file_ids`/`metadata` are **null** (not `[]`), `db_id` present and equals `$.data.db_message_id` of the send response. Verified: `text` is 100 chars while `html` is the full 184-char body.
- `$.delivery_id` (string) is a per-delivery id; headers `X-DialogBrain-Event`, `X-DialogBrain-Signature`.
- Attachments, **from code, not captured** (`message_event_service.py`): `attachments[] = {file_id, filename, mime_type, download_url, media_type}`.
- `message.updated`, **from code, not captured** (an edit of a livechat message produced no event; `messages.edit` on livechat only returns `status: edited` and does not change `read_history`): `channel:message:edited` is published by channel-sync-service. Some producers send only a partial message (`message_id, db_id, channel, peer` + `meta` or `attachment_file_ids`/`attachments`, no `html`/`text`), see `webhook_message_updated_derived.json`. Handle absent `html` by not overwriting the stored text.
- `message.deleted`: `$.data.message_ids` (list of channel refs, e.g. `telegram:25798`), NO `db_id`; `$.data.thread_id` double-prefixed (above).
- Webhooks are workspace-wide: the capture subscription also received real inbound Telegram traffic of the workspace. A subscriber receives events for all channels, not only the one you test.

## search.threads (`search_threads.json`)

- Shape: `$.data.{goal, mode, threads[], count, total_count, offset, has_more}`. `since` is a date; `limit` honored; `offset` supported.
- **There is NO per-thread last-activity timestamp** in the list result (`match_timestamp` is null unless a text query matched). Ordering is `is_pinned DESC, last_message_ts DESC` (server side), so use list order. The timestamp exists in `messages.read_history` -> `$.data.thread.last_message_ts` and in the webhook `$.data.thread.last_message_ts`. Other per-thread fields: `last_message_preview` (truncated), `last_message_is_outgoing`, `last_sender`, `unread_count`, `needs_reply`, `has_real_outgoing`.
- The tool id `threads.list` does NOT exist (404 "not found in registry").

## messages.read_history (`read_history_page*.json`, `read_history_full.json`)

- `$.data.{thread{id,title,channel,last_message_ts,account_id,synced}, messages[], total_returned, offset, has_more, next_offset}`.
- **Order is newest first** (page1 with `limit=1` returns the latest message). Paging: `offset` in, `next_offset` out (`null` on the last page, else int), `has_more`.
- `messages[i]`: `id` (int, == webhook `db_id`), `text` (full body), `sent_at` (`"2026-11-13 12:06:51.597772+00:00"` - **space-separated, microseconds**, unlike the webhook), `is_outgoing`, `is_edited`, `message_type`, `link`, `sender{id,name,username}` (`id` null for livechat and own sends; own sends have name `"You"`), `attachment_file_ids`, `has_media`, `meta{source,...}`, `channel`, `reply_to`. **No `html`, no channel `message_id`, no `from_id`** in history.

## messages.send (`messages_send*.json`)

- Params: `{"thread_id": "<id or channel ref>", "text": "..."}`.
- `$.data`: `message_id` (channel ref `livechat:msg_...`), **`db_message_id`** (int), `thread_id` (int), `channel_ref`, `channel`, `text`, `text_preview`, `sent_at` (`"2026-11-13T12:06:38.977415"`, naive, no tz - differs from the echo `sent_at` `...38.865500+00:00`), `account_id` (string), `sent_from{...}`, `attachments_sent`. `effects_applied` includes `message_sent`.
- The webhook echo for the same send has `db_id == str(db_message_id)`.

## Webhook API (`webhooks_*.json`)

- Create: `POST /api/v1/webhooks {url, event_types[], agent_id?}` -> `{id (int), url, event_types, active, created_at, agent_id, secret}`; **no SSRF/private-URL guard** (a `http://host.docker.internal:8799` receiver was accepted and delivered to).
- List only shows **active** subscriptions. `DELETE` returns 204 and is a soft delete (`active = false`), so it disappears from the list. Deliveries: `[{id, event_type, status, response_status, attempt, max_attempts, created_at, delivered_at, next_attempt_at}]`, newest first.
- Event types used in capture: `message.received`, `message.deleted`. `conversation.*` events did not fire for the test flows.
- A simulated inbound (`agents_simulate_inbound`) does NOT fire `message.received`.

## Tools list (R3)

`GET /api/tools/catalog` IS reachable with an API key (329 tools; `{tools[{id,name,description,category,effects,tags,version,can_undo,idempotent,requires_approval,can_auto_execute}], total, mode, categories, statistics, default_agent_tools}`). `list_tools()` therefore returns the set of ids. (Not `GET /api/tools`.)

## Errors

- Bad key: HTTP 401, body `{"detail": {"code": "INVALID_API_KEY", "message": "Invalid API key"}}` (detail is an object, not a string).
- Tool failure: HTTP 200 with `{"success": false, "data": null, "error": "<string>"}`; unknown tool: HTTP 404 `{"detail": "Tool '...' not found in registry"}`; needing approval: 200 `{"success": false, "requires_approval": true, "message": ...}`.
