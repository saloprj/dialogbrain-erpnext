"""Idempotent upserts of DialogBrain channels, conversations and messages.

Shared by the webhook processor, sending, backfill and reconcile.

Thread identity
---------------
``DB Conversation.thread_id`` (unique) is DialogBrain's *internal* thread id (e.g.
``"8451249"``): it is globally unique and accepted by ``messages.send`` /
``messages.read_history``. ``thread_ref`` is the channel ref (``telegram:100000001``),
which is NOT unique across channel accounts (two Telegram accounts talking to the same
person share it), so it is only looked up together with the channel.

Some events carry only the ref (``messages.send`` echoes without a parsable id,
``message.updated`` partials, ``conversation.updated``). A conversation first seen that
way has ``thread_id = NULL`` and is found by ``(channel, thread_ref)``, which has a
composite UNIQUE index (a concurrent second insert collides and re-reads instead of
splitting the thread). ``thread_id`` is filled as soon as an event carries it.

Message completeness
--------------------
A partial ``message.updated`` for an unknown ``db_id`` (no ``is_outgoing``) is stored as
a *provisional* row (``direction`` empty): no counters, realtime or notification. The
first full event for that ``db_id`` completes it and is reported as ``created=True`` so
the caller applies counters exactly once.
"""

import html as html_lib
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from dialogbrain.instance import current_instance
from dialogbrain.party import contact_keys_from_ref, link_party
from dialogbrain.sync.notify import notify, publish

ECHO_WINDOW_SECONDS = 120
ECHO_STATUSES = ("pending", "unknown", "sent")
PREVIEW_LENGTH = 140
CONTACT_FIELDS = ("contact_name", "phone", "email", "handle", "avatar_url", "db_contact_id")
DUPLICATE_ERRORS = (frappe.DuplicateEntryError, frappe.UniqueValidationError)


# --- helpers ----------------------------------------------------------------


def _s(value):
	"""String or None (never "")."""
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def to_system_datetime(value):
	"""DialogBrain timestamp -> naive datetime in the site's system timezone.

	Strings without a timezone are UTC (DialogBrain's ``messages.send`` response is naive
	UTC); naive ``datetime`` objects are assumed to be system time already.
	"""
	if not value:
		return None
	if isinstance(value, datetime):
		if value.tzinfo is None:
			return value
		dt = value
	else:
		text = str(value).strip()
		if text.endswith("Z"):
			text = text[:-1] + "+00:00"
		try:
			dt = datetime.fromisoformat(text)
		except ValueError:
			dt = get_datetime(text)
		if dt.tzinfo is None:
			dt = dt.replace(tzinfo=timezone.utc)
	return dt.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)


def plain_text(value):
	text = html_lib.unescape(re.sub(r"<[^>]+>", " ", value or ""))
	return re.sub(r"\s+", " ", text).strip()


def _preview(value):
	return plain_text(value)[:PREVIEW_LENGTH]


def rollback_to(save_point: str) -> None:
	"""Roll back to ``save_point``; if the database already ended the transaction (a
	deadlock or lock-wait timeout rolls back everything, savepoints included), roll back
	the rest so the caller can go on with a clean transaction."""
	try:
		frappe.db.rollback(save_point=save_point)
	except Exception:  # noqa: BLE001 - "SAVEPOINT ... does not exist"
		frappe.db.rollback()


class ConversationRaceLost(Exception):
	"""Another transaction created the conversation after this transaction's snapshot.

	At REPEATABLE READ (MariaDB, and Frappe's Postgres connection) the winner's row stays
	invisible to every later plain read of this transaction, Frappe's link validation
	included. Retry in a fresh transaction (``commit`` first)."""


RETRYABLE_DB_ERRORS = (frappe.QueryDeadlockError, frappe.QueryTimeoutError, ConversationRaceLost)


def _insert(doc):
	"""Insert; on a duplicate key return False and leave the transaction usable."""
	save_point = "dialogbrain_ins_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(save_point)
	log_len = len(frappe.local.message_log or [])
	try:
		doc.insert(ignore_permissions=True)
		return True
	except DUPLICATE_ERRORS:
		frappe.db.rollback(save_point=save_point)
		# drop the "must be unique" msgprint Frappe queued for the UI
		if frappe.local.message_log:
			del frappe.local.message_log[log_len:]
		return False


# --- channel ----------------------------------------------------------------


def upsert_channel(account_id: str, channel_type: str, title: "str | None" = None) -> str:
	account_id = _s(account_id)
	if not account_id:
		raise ValueError("DialogBrain channel account_id is required")
	current = frappe.db.get_value("DB Channel", account_id, ["channel_type", "title"], as_dict=True)
	if current:
		updates = {}
		if channel_type and current.channel_type != channel_type:
			updates["channel_type"] = channel_type
		if title and current.title != title:
			updates["title"] = title
		if updates:
			frappe.db.set_value("DB Channel", account_id, updates)
		return account_id
	_insert(
		frappe.get_doc(
			{
				"doctype": "DB Channel",
				"account_id": account_id,
				"channel_type": channel_type,
				"title": title or f"{channel_type or 'channel'} {account_id}",
			}
		)
	)
	return account_id


# --- conversation -----------------------------------------------------------


def thread_keys(thread: dict):
	"""Return ``(internal_id, channel_ref)`` from any DialogBrain thread shape.

	Accepts webhook ``thread`` objects (``thread_id`` = ref, ``db_id``), search results
	(``id``/``thread_id`` = internal, ``channel_ref``), ``read_history.thread`` (``id``)
	and event data (``thread_id`` internal int + ``thread_ref``). A purely numeric
	``thread_id`` is the internal id, anything else is a channel ref.
	"""
	internal = _s(thread.get("db_id")) or _s(thread.get("id"))
	ref = _s(thread.get("channel_ref")) or _s(thread.get("thread_ref"))
	tid = _s(thread.get("thread_id"))
	if tid:
		if tid.isdigit():
			internal = internal or tid
		else:
			ref = ref or tid
	return internal, ref


def find_conversation(internal=None, ref=None, channel=None, for_update=False):
	"""Find a conversation by internal id, else by ``(channel, thread_ref)``.

	Without ``channel`` a ref matches only when exactly one conversation has it.
	``for_update``: a locking read, which sees rows committed after this transaction's
	snapshot (needed after losing an insert race).
	"""
	internal, ref = _s(internal), _s(ref)
	if internal:
		name = frappe.db.get_value("DB Conversation", {"thread_id": internal}, for_update=for_update)
		if name:
			return name
	if ref:
		if channel:
			return frappe.db.get_value(
				"DB Conversation", {"channel": channel, "thread_ref": ref}, for_update=for_update
			)
		names = frappe.db.sql_list(
			"select name from `tabDB Conversation` where thread_ref = %s limit 2"
			+ (" for update" if for_update else ""),
			(ref,),
		)
		if len(names) == 1:
			return names[0]
	return None


DATA_LENGTH = 140  # Frappe Data column
TRUNCATABLE = ("title", "contact_name")


def _fit(field, value):
	"""Display names are cut to the Data length; an over-long id/URL/email is dropped
	(a cut one would be wrong). DialogBrain titles of email groups can exceed 140."""
	if value is None or len(value) <= DATA_LENGTH:
		return value
	return value[:DATA_LENGTH] if field in TRUNCATABLE else None


def upsert_conversation(thread: dict, channel: str) -> "tuple[str, bool]":
	"""Create or update a DB Conversation from a DialogBrain thread dict.

	Besides the id keys (see :func:`thread_keys`) it reads ``title`` and the optional
	contact fields ``contact_name, phone, email, handle, avatar_url, db_contact_id``.
	A phone / email the channel ref carries (:func:`contact_keys_from_ref`: WhatsApp and
	Viber DMs, email DMs) fills a missing one, so backfill and reconcile, which get no
	contact metadata, link parties too. ``link_party`` runs on create and whenever
	phone/email change.

	``dialogbrain_instance`` (final wave F3b) is set to the current connection's instance
	key on create, and on an existing row that has none; a row stamped by another instance
	keeps its key (the agent tools then do not identify it).
	"""
	internal, ref = thread_keys(thread)
	if not internal and not (ref and channel):
		raise ValueError("DialogBrain thread without an internal id or a channel ref + channel")
	values = {"title": _s(thread.get("title"))}
	for field in CONTACT_FIELDS:
		values[field] = _s(thread.get(field))
	values = {k: _fit(k, v) for k, v in values.items() if v}
	values = {k: v for k, v in values.items() if v}

	name = find_conversation(internal, ref, channel)
	if not name:
		for field, value in contact_keys_from_ref(ref).items():
			values.setdefault(field, _fit(field, value))
		values = {k: v for k, v in values.items() if v}
		doc = frappe.get_doc(
			{
				"doctype": "DB Conversation",
				"thread_id": internal,
				"thread_ref": ref,
				"channel": channel,
				"status": "Open",
				"unread_count": 0,
				"dialogbrain_instance": current_instance(),
				**values,
			}
		)
		if _insert(doc):
			link_party(doc.name)
			return doc.name, True
		# another worker created it meanwhile (committed after our snapshot: locking read)
		name = find_conversation(internal, ref, channel, for_update=True)
		if not name:
			raise frappe.DuplicateEntryError("DB Conversation", internal or ref)
		if not frappe.db.sql("select name from `tabDB Conversation` where name = %s", (name,)):
			raise ConversationRaceLost(f"DB Conversation {name} was created by a concurrent transaction")
		# the winner may not know the internal id yet
		if internal and not frappe.db.get_value("DB Conversation", name, "thread_id"):
			frappe.db.set_value("DB Conversation", name, "thread_id", internal)
		return name, False

	fields = {"thread_id", "thread_ref", "channel", "phone", "email", "dialogbrain_instance", *values.keys()}
	current = frappe.db.get_value("DB Conversation", name, sorted(fields), as_dict=True)
	for field, value in contact_keys_from_ref(ref or current.thread_ref).items():
		if field not in values and not current.get(field):
			values[field] = _fit(field, value)
	values = {k: v for k, v in values.items() if v}
	updates = {k: v for k, v in values.items() if current.get(k) != v}
	if not current.dialogbrain_instance and (instance := current_instance()):
		updates["dialogbrain_instance"] = instance
	if internal and current.thread_id != internal:
		updates["thread_id"] = internal  # fill (or follow a re-created DialogBrain thread)
	new_ref = ref if ref and not current.thread_ref else None
	new_channel = channel if channel and not current.channel else None
	if new_ref or new_channel:
		holder = _ref_holder(name, current.channel or channel, current.thread_ref or ref)
		if holder and _mergeable(holder, internal or current.thread_id):
			_merge_conversation(name, holder)
		elif holder:  # another DialogBrain thread holds this ref: keep ours without it
			new_ref = new_channel = None
		if new_ref:
			updates["thread_ref"] = new_ref
		if new_channel:
			updates["channel"] = new_channel
	if updates:
		frappe.db.set_value("DB Conversation", name, updates)
		if "phone" in updates or "email" in updates:
			link_party(name)
	return name, False


# --- merging a ref-only duplicate ---------------------------------------------

# Rows that point at a conversation (re-pointed when two rows are merged).
CONVERSATION_LINKS = (
	("DB Message", "conversation", None),
	("DB Draft", "conversation", None),
	("Notification Log", "document_name", "document_type"),
	("Comment", "reference_name", "reference_doctype"),
	("ToDo", "reference_name", "reference_type"),
	("File", "attached_to_name", "attached_to_doctype"),
	("DB Agent Call Log", "conversation", None),
)
# Documents the AI agent created point back at their chat (``install.DB_CONVERSATION_DOCTYPES``);
# the custom field may be absent (e.g. mid-uninstall), so each is checked first.
DB_CONVERSATION_FIELD_DOCTYPES = ("Sales Order", "Quotation", "Lead")
MERGE_FIELDS = (
	"title",
	*CONTACT_FIELDS,
	"party_type",
	"party",
	"contact",
	"link_source",
	"assigned_to",
	"needs_human",
	"status",
	"agent_override",
	"unread_count",
	"last_message_at",
	"last_message_preview",
	"last_message_direction",
	"last_inbound_at",
	"opened_at",
	"closed_at",
	"first_response_seconds",
)


def _ref_holder(name, channel, ref):
	"""Another conversation holding ``(channel, ref)`` (locking read: sees committed rows)."""
	if not (channel and ref):
		return None
	return frappe.db.get_value(
		"DB Conversation",
		{"channel": channel, "thread_ref": ref, "name": ["!=", name]},
		for_update=True,
	)


def _mergeable(holder, internal):
	"""The holder is the same thread when it has no internal id (or the same one)."""
	holder_id = frappe.db.get_value("DB Conversation", holder, "thread_id")
	return not holder_id or holder_id == internal


def _merge_conversation(target, source):
	"""Fold ``source`` (a ref-only duplicate of the same thread) into ``target`` and delete
	it, in the caller's transaction. Messages, drafts, notifications, comments, ToDos,
	files, agent call logs and the agent's documents (``db_conversation``) are re-pointed; party link, assignment and contact details are taken from
	``source`` where ``target`` has none; counters are combined."""
	for doctype, field, type_field in CONVERSATION_LINKS:
		where = f"`{field}` = %(source)s"
		if type_field:
			where += f" and `{type_field}` = 'DB Conversation'"
		frappe.db.sql(
			f"update `tab{doctype}` set `{field}` = %(target)s where {where}",
			{"target": target, "source": source},
		)
	for doctype in DB_CONVERSATION_FIELD_DOCTYPES:
		if frappe.db.has_column(doctype, "db_conversation"):
			frappe.db.sql(
				f"update `tab{doctype}` set `db_conversation` = %(target)s where `db_conversation` = %(source)s",
				{"target": target, "source": source},
			)

	tgt = frappe.db.get_value("DB Conversation", target, list(MERGE_FIELDS), as_dict=True)
	src = frappe.db.get_value("DB Conversation", source, list(MERGE_FIELDS), as_dict=True)
	updates = {}
	for field in ("title", *CONTACT_FIELDS, "assigned_to", "opened_at", "first_response_seconds"):
		if not tgt.get(field) and src.get(field):
			updates[field] = src.get(field)
	if src.party and (not tgt.party or (src.link_source == "manual" and tgt.link_source != "manual")):
		updates.update({f: src.get(f) for f in ("party_type", "party", "contact", "link_source")})
	if src.needs_human and not tgt.needs_human:
		updates["needs_human"] = 1
	if (tgt.agent_override or "default") == "default" and (src.agent_override or "default") != "default":
		updates["agent_override"] = src.agent_override
	if src.status == "Open" and tgt.status == "Closed":
		updates.update({"status": "Open", "closed_at": None})
	if src.unread_count:
		updates["unread_count"] = (tgt.unread_count or 0) + src.unread_count
	if src.last_message_at and (
		not tgt.last_message_at or get_datetime(src.last_message_at) > get_datetime(tgt.last_message_at)
	):
		for field in ("last_message_at", "last_message_preview", "last_message_direction"):
			updates[field] = src.get(field)
	if src.last_inbound_at and (
		not tgt.last_inbound_at or get_datetime(src.last_inbound_at) > get_datetime(tgt.last_inbound_at)
	):
		updates["last_inbound_at"] = src.last_inbound_at

	frappe.db.delete("DB Conversation", {"name": source})  # frees (channel, thread_ref)
	if updates:
		frappe.db.set_value("DB Conversation", target, updates)


# --- message ----------------------------------------------------------------


def _message_by_db_id(db_id):
	return frappe.db.get_value("DB Message", {"db_id": db_id}) if db_id else None


def _attachments(message):
	items = message.get("attachments")
	if not isinstance(items, list):
		return None
	rows = []
	for item in items:
		if not isinstance(item, dict):
			continue
		rows.append(
			{
				"file_id": _s(item.get("file_id") or item.get("id")),
				"filename": _s(item.get("filename") or item.get("name")),
				"mime_type": _s(item.get("mime_type")),
				"media_type": _s(item.get("media_type")),
				"size": item.get("size") if isinstance(item.get("size"), int) else None,
			}
		)
	return rows


def _raw(message):
	"""The message without download URLs (they are bearer links)."""
	raw = dict(message)
	if isinstance(raw.get("attachments"), list):
		raw["attachments"] = [
			{k: v for k, v in a.items() if k not in ("download_url", "url")} if isinstance(a, dict) else a
			for a in raw["attachments"]
		]
	return raw


def _parse(message):
	"""Normalise a webhook message or a ``read_history`` message."""
	history_shape = "db_id" not in message and "id" in message
	sender = message.get("sender") or {}
	body = message.get("html")
	partial_text = None
	if body is None:
		if history_shape:
			body = message.get("text")  # read_history text is the full body
		else:
			partial_text = message.get("text")  # webhook text is truncated
	return {
		"db_id": _s(message.get("db_id")) or (_s(message.get("id")) if history_shape else None),
		"message_id": _s(message.get("message_id")),
		"is_outgoing": message.get("is_outgoing"),
		"body": body,
		"partial_text": partial_text,
		"from_id": _s(message.get("from_id")) or _s(sender.get("id")),
		"from_name": _s(message.get("from_name")) or _s(sender.get("name")),
		"sent_at": to_system_datetime(message.get("sent_at")),
		"is_system_event": bool(message.get("is_system_event")),
		"attachments": _attachments(message),
	}


def _author_type(parsed):
	if not parsed["is_outgoing"]:
		return "system" if parsed["is_system_event"] else "customer"
	if parsed["from_id"] == "system" and (parsed["from_name"] or "") not in ("You", ""):
		return "agent"
	return "manager"


def _same_text(a, b):
	return (a or "") == (b or "") or plain_text(a) == plain_text(b)


def _find_echo_target(conversation, parsed):
	if not parsed["is_outgoing"] or parsed["body"] is None:
		return None
	sent_at = parsed["sent_at"] or now_datetime()
	candidates = frappe.get_all(
		"DB Message",
		filters={
			"conversation": conversation,
			"direction": "out",
			"db_id": ["is", "not set"],
			"delivery_status": ["in", list(ECHO_STATUSES)],
		},
		fields=["name", "text", "sent_at", "raw"],
		order_by="creation asc",
	)
	for row in candidates:
		if not _same_text(row.text, parsed["body"]):
			continue
		for at in _echo_anchors(row):
			if abs((at - sent_at).total_seconds()) <= ECHO_WINDOW_SECONDS:
				return row.name
	return None


def _echo_anchors(row):
	"""When the reply may have gone out: ``sent_at`` (written when the reply was created)
	and, for a retried reply, its attempts (``raw.claimed_at`` / ``raw.send.attempt_at``):
	the echo of a retry hours later comes near the retry, not near ``sent_at`` (F2d)."""
	import json

	values = [row.sent_at]
	raw = row.raw
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			raw = None
	if isinstance(raw, dict):
		send = raw.get("send") if isinstance(raw.get("send"), dict) else {}
		values += [raw.get("claimed_at"), send.get("attempt_at")]
	anchors = []
	for value in values:
		try:
			if value:
				anchors.append(get_datetime(value))
		except Exception:  # noqa: BLE001 - unreadable stamp: ignored
			pass
	return anchors


def upsert_message(conversation: str, message: dict, *, silent: bool = False) -> "tuple[str, bool]":
	"""Insert or update a DB Message keyed by DialogBrain ``db_id``.

	Returns ``(name, created)``; ``created`` is True when the row became a complete
	message now (new row, or a provisional row completed) and the caller must apply
	counters (:func:`apply_message_counters`). An outgoing echo matched to a pending
	ERPNext reply is merged into that row (``created=False``) and its counters are
	applied here. A message without ``is_outgoing`` is stored provisional (see module
	docstring) and returns ``created=False``.
	"""
	parsed = _parse(message)
	db_id = parsed["db_id"]

	existing = _message_by_db_id(db_id)
	provisional = bool(existing) and not frappe.db.get_value("DB Message", existing, "direction")
	if existing and not provisional:
		_update_message(existing, parsed, silent=silent)
		return existing, False

	if parsed["is_outgoing"] is None:
		if existing:
			_update_message(existing, parsed, silent=True)
			return existing, False
		return _insert_message(conversation, parsed, message, provisional=True), False

	echo = _find_echo_target(conversation, parsed) if db_id else None
	if echo:
		if existing:  # the provisional row is the same message: fold it into the reply
			frappe.delete_doc("DB Message", existing, ignore_permissions=True, force=True)
		doc = frappe.get_doc("DB Message", echo)
		doc.db_id = db_id
		doc.message_id = parsed["message_id"] or doc.message_id
		doc.author_type = "manager"  # sent_by is kept
		if parsed["sent_at"]:
			doc.sent_at = parsed["sent_at"]
		if doc.delivery_status in ("pending", "unknown"):
			doc.delivery_status = "sent"
		doc.raw = _raw(message)
		doc.save(ignore_permissions=True)
		apply_message_counters(conversation, doc.name, silent=silent)
		return doc.name, False

	if existing:  # complete the provisional row
		_update_message(existing, parsed, silent=True, extra=_direction_fields(parsed, message))
		return existing, True

	name = _insert_message(conversation, parsed, message)
	if name:
		return name, True
	existing = _message_by_db_id(db_id)
	if not existing:
		raise frappe.DuplicateEntryError("DB Message", db_id)
	return existing, False


def _direction_fields(parsed, message):
	direction = "out" if parsed["is_outgoing"] else "in"
	return {
		"direction": direction,
		"author_type": _author_type(parsed),
		"delivery_status": "sent" if direction == "out" else None,
		"raw": _raw(message),
	}


def _insert_message(conversation, parsed, message, *, provisional=False):
	"""Insert; returns the name, or None on a duplicate ``db_id`` (concurrent insert)."""
	values = {
		"doctype": "DB Message",
		"conversation": conversation,
		"db_id": parsed["db_id"],
		"message_id": parsed["message_id"],
		"text": parsed["body"] if parsed["body"] is not None else parsed["partial_text"],
		"sent_at": parsed["sent_at"],
		"attachments": parsed["attachments"] or [],
		"raw": _raw(message),
	}
	if not provisional:
		values.update(_direction_fields(parsed, message))
	doc = frappe.get_doc(values)
	doc.flags.dialogbrain_provisional = provisional
	if _insert(doc):
		return doc.name
	if provisional:
		return _message_by_db_id(parsed["db_id"])
	return None


def _update_message(name, parsed, *, silent, extra=None):
	doc = frappe.get_doc("DB Message", name)
	doc.flags.dialogbrain_provisional = not doc.direction and not (extra or {}).get("direction")
	changed = False
	for field, value in (extra or {}).items():
		if doc.get(field) != value:
			doc.set(field, value)
			changed = True

	def put(field, value):
		nonlocal changed
		if value is not None and doc.get(field) != value:
			doc.set(field, value)
			changed = True

	if parsed["body"] is not None:
		put("text", parsed["body"])
	elif parsed["partial_text"] and not doc.text:
		put("text", parsed["partial_text"])
	put("message_id", parsed["message_id"])
	if parsed["sent_at"] and (not doc.sent_at or get_datetime(doc.sent_at) != parsed["sent_at"]):
		put("sent_at", parsed["sent_at"])
	if parsed["attachments"] and [a["file_id"] for a in parsed["attachments"]] != [
		a.file_id for a in doc.attachments
	]:
		doc.set("attachments", parsed["attachments"])
		changed = True
	if not changed:
		return
	doc.save(ignore_permissions=True)
	if not silent:
		publish(doc.conversation, "message_updated", {"message": name})


# --- counters ---------------------------------------------------------------


def apply_message_counters(conversation: str, message_name: str, *, silent: bool = False) -> None:
	msg = frappe.db.get_value(
		"DB Message", message_name, ["direction", "author_type", "text", "sent_at"], as_dict=True
	)
	conv = frappe.db.get_value(
		"DB Conversation",
		conversation,
		[
			"status",
			"last_message_at",
			"last_inbound_at",
			"unread_count",
			"opened_at",
			"closed_at",
			"first_response_seconds",
		],
		as_dict=True,
		for_update=True,
	)
	if not msg or not conv:
		return

	sent_at = get_datetime(msg.sent_at) if msg.sent_at else None
	inbound = msg.direction == "in"
	updates = {}

	if sent_at and (not conv.last_message_at or sent_at >= get_datetime(conv.last_message_at)):
		updates["last_message_at"] = sent_at
		updates["last_message_preview"] = _preview(msg.text)
		updates["last_message_direction"] = msg.direction

	first_inbound = False
	if inbound:
		# Unread = what a manager still has to read: not re-emitted/imported history
		# (``silent``) and not system events (joined, left, call placeholders).
		if not silent and msg.author_type != "system":
			updates["unread_count"] = (conv.unread_count or 0) + 1
		if sent_at and (not conv.last_inbound_at or sent_at > get_datetime(conv.last_inbound_at)):
			updates["last_inbound_at"] = sent_at
		closed_before = conv.closed_at and sent_at and sent_at < get_datetime(conv.closed_at)
		if conv.status == "Closed" and not closed_before:
			updates.update(
				{"status": "Open", "opened_at": sent_at or now_datetime(), "first_response_seconds": 0}
			)
			first_inbound = True
		elif not frappe.db.exists(
			"DB Message", {"conversation": conversation, "direction": "in", "name": ["!=", message_name]}
		):
			first_inbound = True
			if not conv.opened_at:
				updates["opened_at"] = sent_at or now_datetime()
	elif (
		not conv.first_response_seconds  # Int column: 0 means "not measured yet"
		and conv.opened_at
		and sent_at
		and sent_at >= get_datetime(conv.opened_at)
	):
		seconds = int((sent_at - get_datetime(conv.opened_at)).total_seconds())
		updates["first_response_seconds"] = max(seconds, 1)  # 0 is reserved for "unset"

	if updates:
		frappe.db.set_value("DB Conversation", conversation, updates)

	if silent:
		return
	publish(conversation, "message", {"message": message_name, "direction": msg.direction})
	if inbound:
		notify(conversation, "first_inbound" if first_inbound else "inbound")
