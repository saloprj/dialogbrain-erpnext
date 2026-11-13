"""Process stored DialogBrain webhook deliveries (DB Webhook Log) into DB rows.

Payload shapes: see ``dialogbrain/tests/fixtures/README.md`` (captured) and, for events
not captured, DialogBrain's publishers (``conversation_event_service``,
``handoff_event_service``, ``auto_reply_event_service.DraftCreatedPayload``,
``contact_event_service``).
"""

import json

import frappe

from dialogbrain.party import link_party
from dialogbrain.sync.notify import notify, publish
from dialogbrain.sync.upsert import (
	_insert,
	_s,
	apply_message_counters,
	find_conversation,
	thread_keys,
	to_system_datetime,
	upsert_channel,
	upsert_conversation,
	upsert_message,
)

MAX_ERROR_LENGTH = 4000


MAX_ATTEMPTS = 5
PROCESSABLE = ("queued", "failed")


def delivery_job_id(log_name: str) -> str:
	"""One RQ job id per delivery: the receiver and the retry dedupe on it."""
	return f"dialogbrain_delivery_{log_name}"


def _commit():
	"""Commit (skipped under tests, which rely on rollback for isolation)."""
	if not frappe.flags.in_test:
		frappe.db.commit()


def process_delivery(log_name: str) -> None:
	"""Background job: process one DB Webhook Log row, at most once.

	1. Lock the row; only ``queued``/``failed`` rows with ``attempts < 5`` proceed.
	   ``attempts += 1`` is committed BEFORE processing, so a killed job still counts
	   (the retry gives up after 5) and is never retried forever.
	2. Re-lock the row for the whole processing; a concurrent job blocks here and then
	   sees ``processed`` and returns.
	3. Success -> ``processed``; any error -> the delivery's partial writes are rolled back
	   and the row is ``failed`` with the error (retried by the scheduler). Never raises.
	"""
	log = frappe.db.get_value(
		"DB Webhook Log", log_name, ["name", "status", "attempts"], as_dict=True, for_update=True
	)
	if not log or log.status not in PROCESSABLE or (log.attempts or 0) >= MAX_ATTEMPTS:
		return
	attempts = (log.attempts or 0) + 1
	frappe.db.set_value("DB Webhook Log", log_name, "attempts", attempts, update_modified=False)
	_commit()

	log = frappe.db.get_value(
		"DB Webhook Log", log_name, ["name", "event", "payload", "status"], as_dict=True, for_update=True
	)
	if not log or log.status not in PROCESSABLE:
		return
	save_point = "dialogbrain_delivery"
	frappe.db.savepoint(save_point)
	try:
		body = log.payload
		if isinstance(body, str):
			body = json.loads(body)
		body = body or {}
		process_event(body.get("event") or log.event, body.get("data") or {})
	except Exception:  # noqa: BLE001 - any failure marks the delivery failed for retry
		frappe.db.rollback(save_point=save_point)
		frappe.db.set_value(
			"DB Webhook Log",
			log_name,
			{"status": "failed", "error": frappe.get_traceback()[-MAX_ERROR_LENGTH:]},
			update_modified=False,
		)
		return
	frappe.db.set_value(
		"DB Webhook Log", log_name, {"status": "processed", "error": None}, update_modified=False
	)


def process_event(event: str, data: dict) -> None:
	handler = HANDLERS.get(event)
	if handler:
		handler(data or {})


# --- helpers ----------------------------------------------------------------


def _channel_from(data, account_key="account_id", type_key="channel"):
	account_id = _s(data.get(account_key))
	if not account_id:
		return None
	return upsert_channel(account_id, _s(data.get(type_key)))


def _strip_duplicate_prefix(ref, channel_type):
	"""``telegram:telegram:-100…`` (message.deleted) -> ``telegram:-100…``."""
	ref = _s(ref)
	if ref and channel_type:
		prefix = channel_type + ":"
		while ref.startswith(prefix + prefix):
			ref = ref[len(prefix) :]
	return ref


def _event_thread(data):
	"""Thread dict for :func:`upsert_conversation` from a message event's ``data``.

	``data.thread_id`` is the channel ref. ``data.thread.db_id`` is the internal id when
	present; a ``messages.send`` echo has no ``db_id`` but ``thread.thread_id`` is
	``<channel>:<internal id>``.
	"""
	channel_type = _s(data.get("channel"))
	ref = _strip_duplicate_prefix(data.get("thread_id"), channel_type)
	thread = data.get("thread") if isinstance(data.get("thread"), dict) else {}
	internal = _s(thread.get("db_id"))
	if not internal:
		tid = _s(thread.get("thread_id"))
		if tid and tid != ref and channel_type and tid.startswith(channel_type + ":"):
			tail = tid[len(channel_type) + 1 :]
			if tail.isdigit():
				internal = tail
	result = {"db_id": internal, "channel_ref": ref or _s(thread.get("thread_id")), "title": thread.get("title")}

	message = data.get("message") or {}
	if message.get("is_outgoing") is False and thread.get("kind") != "group":
		sender = message.get("sender") or {}
		meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
		name = _s(message.get("from_name")) or _s(sender.get("name"))
		result.update(
			{
				"contact_name": name,
				"handle": meta.get("username"),
				"phone": meta.get("phone"),
				"email": meta.get("email"),
			}
		)
		if not result["title"]:
			result["title"] = name
	return result


# --- handlers ---------------------------------------------------------------


def _on_message(data):
	channel = _channel_from(data)
	if not channel:
		raise ValueError("message event without account_id")
	conversation, _ = upsert_conversation(_event_thread(data), channel)
	name, created = upsert_message(conversation, data.get("message") or {})
	if created:
		apply_message_counters(conversation, name)


def _on_message_deleted(data):
	ids = [i for i in (_s(x) for x in (data.get("message_ids") or [])) if i]
	if not ids:
		return
	channel = _s(data.get("account_id"))
	ref = _strip_duplicate_prefix(data.get("thread_id"), _s(data.get("channel")))
	conversation = find_conversation(None, ref, channel) if ref else None

	names = []
	if conversation:
		names = frappe.get_all(
			"DB Message", filters={"conversation": conversation, "message_id": ["in", ids]}, pluck="name"
		)
	if not names and channel:
		names = frappe.get_all(
			"DB Message",
			filters={
				"message_id": ["in", ids],
				"conversation": ["in", frappe.get_all("DB Conversation", {"channel": channel}, pluck="name") or [""]],
			},
			pluck="name",
		)
	for name in names:
		frappe.db.set_value("DB Message", name, "delivery_status", "deleted")
		publish(frappe.db.get_value("DB Message", name, "conversation"), "message_updated", {"message": name})


def _on_conversation_started(data):
	channel = _channel_from(data)
	if not channel:
		return
	conversation, _ = upsert_conversation(
		{"thread_id": data.get("thread_id"), "thread_ref": data.get("thread_ref"), "title": data.get("title")},
		channel,
	)
	publish(conversation, "conversation", {})


def _on_conversation_updated(data):
	"""Only updates known conversations: DialogBrain also fires this for threads that
	never get a person's message (calls, placeholders)."""
	channel = _s(data.get("account_id"))
	channel_type = _s(data.get("channel"))
	ref = _s(data.get("thread_ref"))
	if not ref and channel_type and _s(data.get("peer")):
		ref = f"{channel_type}:{_s(data.get('peer'))}"
	internal = _s(data.get("thread_id"))
	if internal and not internal.isdigit():
		internal, ref = None, ref or internal
	conversation = find_conversation(internal, ref, channel)
	if not conversation:
		return
	current_channel = frappe.db.get_value("DB Conversation", conversation, "channel")
	upsert_conversation(
		{"db_id": internal, "channel_ref": ref, "title": data.get("title")}, channel or current_channel
	)
	publish(conversation, "conversation", {})


def _on_contact_updated(data):
	contact_id = _s(data.get("contact_id"))
	if not contact_id:
		return
	display_name = _s(data.get("display_name"))
	for name in frappe.get_all("DB Conversation", {"db_contact_id": contact_id}, pluck="name"):
		if display_name:
			frappe.db.set_value("DB Conversation", name, "contact_name", display_name)
		link_party(name)
		publish(name, "conversation", {})


def _on_draft_created(data):
	draft_id = _s(data.get("draft_id"))
	if not draft_id:
		return
	thread = {"thread_id": data.get("thread_id"), "channel_ref": data.get("channel_ref"), "title": data.get("thread_title")}
	channel = _channel_from(data, account_key="channel_account_id", type_key="channel_type")
	if channel:
		conversation, _ = upsert_conversation(thread, channel)
	else:
		conversation = find_conversation(*thread_keys(thread))
	if not conversation:
		raise LookupError(f"draft.created for unknown thread {thread.get('thread_id')}")
	text = data.get("response_text") or data.get("text")

	existing = frappe.db.get_value("DB Draft", {"draft_id": draft_id})
	if existing:
		return

	doc = frappe.get_doc(
		{
			"doctype": "DB Draft",
			"draft_id": draft_id,
			"conversation": conversation,
			"text": text,
			"created_at": to_system_datetime(data.get("created_at")) or frappe.utils.now_datetime(),
			"status": "pending",
		}
	)
	if not _insert(doc):
		return
	for name in frappe.get_all(
		"DB Draft",
		{"conversation": conversation, "status": "pending", "name": ["!=", doc.name]},
		pluck="name",
	):
		frappe.db.set_value("DB Draft", name, "status", "superseded")
	publish(conversation, "draft", {"draft": doc.name})


def _on_handoff_requested(data):
	internal = _s(data.get("thread_id"))
	conversation = find_conversation(internal, _s(data.get("thread_ref")), _s(data.get("account_id")))
	if not conversation:
		# not mirrored yet (webhook before the first message / backfill): fail the
		# delivery so it is retried instead of losing the handoff
		raise LookupError(f"handoff.requested for unknown thread {internal}")
	frappe.db.set_value("DB Conversation", conversation, "needs_human", 1)
	publish(conversation, "conversation", {"needs_human": 1})
	notify(conversation, "handoff")


HANDLERS = {
	"message.received": _on_message,
	"message.updated": _on_message,
	"message.deleted": _on_message_deleted,
	"conversation.started": _on_conversation_started,
	"conversation.updated": _on_conversation_updated,
	"contact.updated": _on_contact_updated,
	"draft.created": _on_draft_created,
	"handoff.requested": _on_handoff_requested,
}
