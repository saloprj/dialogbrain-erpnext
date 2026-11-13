"""Manager replies: store a pending DB Message, deliver it to DialogBrain in a job.

Never send twice
----------------
* ``client_ref`` (unique) makes :func:`create_reply` idempotent: a double click or a
  re-submitted request returns the existing row and enqueues nothing.
* :func:`deliver` first *claims* the row atomically (``pending`` -> ``unknown``, committed)
  and only then calls ``messages.send``, once. A second job, or a re-run after a killed
  worker, finds ``unknown`` and sends nothing. There is no ``request_id`` passthrough in
  DialogBrain yet, so an ambiguous outcome (read timeout, 502/504 from the gateway,
  unexpected exception after the request left, a crash) leaves the row ``unknown``: the
  webhook echo (``upsert_message`` echo matching accepts ``unknown``) or reconcile resolves
  it. While the send is in flight the row is ``unknown`` with an empty ``error``.
* Every outcome write after the claim is conditional (row still ``unknown`` and no
  ``db_id``): an echo that completed the row during the send is never overwritten.
* Only ``failed`` rows without a ``db_id`` (DialogBrain answered with a definite error, or
  nothing was sent) can be retried, via :func:`retry_reply`.

Echo reconciliation (agrees with ``dialogbrain.sync.upsert``)
------------------------------------------------------------
The pending row has ``sent_at`` = now, so an echo that arrives before the send response is
merged into it by ``upsert_message`` (same text, +-120 s). On the response, the row gets
``db_id = db_message_id``; if that ``db_id`` already belongs to another row (an echo that
could not be matched), that row is folded into ours (``sent_by`` kept) and deleted. An echo
arriving after the response finds the row by ``db_id``.
"""

import json
import mimetypes

import frappe
from frappe import _
from frappe.utils import now_datetime

from dialogbrain.client import DialogBrainClient, DialogBrainError, DialogBrainTimeout
from dialogbrain.permissions import assert_can_access
from dialogbrain.sync.notify import publish
from dialogbrain.sync.upsert import (
	_insert,
	_s,
	apply_message_counters,
	to_system_datetime,
)

DELIVER_JOB = "dialogbrain.sync.sender.deliver"
AMBIGUOUS_HTTP_STATUSES = (502, 504)  # the gateway gave up; the send may have happened
MAX_ERROR_LENGTH = 1000


# --- send -------------------------------------------------------------------


def _file_urls(file_urls):
	if not file_urls:
		return []
	if isinstance(file_urls, str):
		file_urls = frappe.parse_json(file_urls)
	if not isinstance(file_urls, (list, tuple)):
		frappe.throw(_("file_urls must be a list"))
	return [u for u in (_s(x) for x in file_urls) if u]


def _assert_can_read_file(file_url, user):
	name = frappe.db.get_value("File", {"file_url": file_url, "is_folder": 0})
	if not name or not frappe.has_permission("File", "read", doc=name, user=user):
		frappe.throw(_("You cannot attach this file"), frappe.PermissionError)


def _existing_reply(client_ref, conversation, user):
	row = frappe.db.get_value(
		"DB Message",
		{"client_ref": client_ref},
		["name", "delivery_status", "conversation", "sent_by"],
		as_dict=True,
	)
	if not row:
		return None
	if row.conversation != conversation or row.sent_by != user:
		frappe.throw(_("client_ref is already used by another message"))
	return {"name": row.name, "delivery_status": row.delivery_status}


def _auto_assign(conversation, user):
	frappe.db.sql(
		"""UPDATE `tabDB Conversation` SET assigned_to = %s
		WHERE name = %s AND (assigned_to IS NULL OR assigned_to = '')""",
		(user, conversation),
	)


def _enqueue(message_name):
	frappe.enqueue(DELIVER_JOB, queue="short", enqueue_after_commit=True, message_name=message_name)


def create_reply(conversation, text, client_ref, file_urls=None, user=None) -> dict:
	"""Insert a pending manager reply and enqueue its delivery (idempotent per ``client_ref``)."""
	user = user or frappe.session.user
	client_ref = _s(client_ref)
	if not client_ref:
		frappe.throw(_("client_ref is required"))
	assert_can_access(conversation, user)

	existing = _existing_reply(client_ref, conversation, user)
	if existing:
		return existing

	text = text or ""
	file_urls = _file_urls(file_urls)
	if not text.strip() and not file_urls:
		frappe.throw(_("Write a message or attach a file"))
	for url in file_urls:
		_assert_can_read_file(url, user)

	doc = frappe.get_doc(
		{
			"doctype": "DB Message",
			"conversation": conversation,
			"direction": "out",
			"author_type": "manager",
			"sent_by": user,
			"text": text,
			"sent_at": now_datetime(),  # the echo window is measured from here
			"delivery_status": "pending",
			"client_ref": client_ref,
			"raw": {"file_urls": file_urls} if file_urls else None,
		}
	)
	if not _insert(doc):  # a concurrent request with the same client_ref won
		existing = _existing_reply(client_ref, conversation, user)
		if not existing:
			raise frappe.DuplicateEntryError("DB Message", client_ref)
		return existing

	_auto_assign(conversation, user)
	publish(conversation, "message", {"message": doc.name, "direction": "out"})
	_enqueue(doc.name)
	return {"name": doc.name, "delivery_status": doc.delivery_status}


def retry_reply(message: str) -> None:
	"""Re-deliver a ``failed`` reply. Anything else (pending/unknown/sent), or a row that
	already has a DialogBrain ``db_id`` (it was delivered), is refused."""
	conversation = frappe.db.get_value("DB Message", message, "conversation")
	if not conversation:
		frappe.throw(_("Message not found"), frappe.DoesNotExistError)
	assert_can_access(conversation)
	row = frappe.db.get_value(
		"DB Message", message, ["delivery_status", "db_id"], as_dict=True, for_update=True
	)
	if row.delivery_status != "failed" or row.db_id:
		frappe.throw(_("Only failed messages can be retried"))
	frappe.db.set_value("DB Message", message, {"delivery_status": "pending", "error": None})
	publish(conversation, "message_updated", {"message": message})
	_enqueue(message)


# --- deliver ----------------------------------------------------------------


def _error_text(exc):
	return (str(exc) or type(exc).__name__)[:MAX_ERROR_LENGTH]


def _commit():
	"""Commit the job's transaction (makes the claim visible and durable, ends the read
	snapshot). Skipped under tests, which rely on rollback for isolation."""
	if not frappe.flags.in_test:
		frappe.db.commit()


def _claim(name):
	"""Atomically move ``pending`` -> ``unknown`` before anything is sent.

	``unknown`` is the honest state while the request is in flight or after a crash: it is
	never sent again automatically, and echo matching accepts it. Returns the row or None
	when another job (or nobody: wrong state) owns it.
	"""
	row = frappe.db.get_value(
		"DB Message",
		name,
		["name", "conversation", "text", "delivery_status", "db_id", "raw"],
		as_dict=True,
		for_update=True,
	)
	if not row or row.delivery_status != "pending" or row.db_id:
		return None
	frappe.db.set_value("DB Message", name, {"delivery_status": "unknown", "error": None})
	_commit()
	return row


def _settle(name, conversation, status, error=None):
	"""Write the job's outcome only if the row is still as the job left it.

	A webhook echo may have completed the row (``db_id`` + ``sent``) while the send was in
	flight; that result must not be overwritten (a ``failed`` row would be retryable =
	sent twice; ``unknown`` with a ``db_id`` would never be matched again).
	"""
	row = frappe.db.get_value("DB Message", name, ["delivery_status", "db_id"], as_dict=True, for_update=True)
	if not row or row.delivery_status != "unknown" or row.db_id:
		return False
	frappe.db.set_value("DB Message", name, {"delivery_status": status, "error": error})
	publish(conversation, "message_updated", {"message": name})
	return True


def _stored_file_urls(raw):
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return []
	return list((raw or {}).get("file_urls") or []) if isinstance(raw, dict) else []


def _upload_files(client, file_urls):
	attachments = []
	for url in file_urls:
		file_doc = frappe.get_doc("File", {"file_url": url, "is_folder": 0})
		with open(file_doc.get_full_path(), "rb") as f:
			content = f.read()
		filename = file_doc.file_name or url.rsplit("/", 1)[-1]
		mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
		file_id = client.upload_file(filename, content, mime_type)
		attachments.append(
			{"file_id": str(file_id), "filename": filename, "mime_type": mime_type, "size": len(content)}
		)
	return attachments


def _send_params(conv, text, attachments):
	if conv.thread_id:
		params = {"thread_id": conv.thread_id}
	else:
		# By channel ref DialogBrain picks "the most recently active account" unless told:
		# pin the account this conversation belongs to.
		params = {"thread_id": conv.thread_ref}
		if conv.channel and str(conv.channel).isdigit():
			params["from_account_id"] = int(conv.channel)
	params["text"] = text or ""
	if attachments:
		params["attachments"] = [int(a["file_id"]) if a["file_id"].isdigit() else a["file_id"] for a in attachments]
	return params


def deliver(message_name: str) -> None:
	"""Background job (queue ``short``): upload files, ``messages.send`` once, record result."""
	msg = _claim(message_name)
	if not msg:
		return
	conv = frappe.db.get_value(
		"DB Conversation", msg.conversation, ["thread_id", "thread_ref", "channel"], as_dict=True
	)

	# Nothing has been sent until messages.send: any failure here is a plain failure.
	try:
		if not conv or not (conv.thread_id or conv.thread_ref):
			raise DialogBrainError("The conversation has no DialogBrain thread id")
		client = DialogBrainClient.from_settings()
		attachments = _upload_files(client, _stored_file_urls(msg.raw))
		params = _send_params(conv, msg.text, attachments)
	except Exception as e:  # noqa: BLE001 - recorded on the row, user can retry
		_settle(message_name, msg.conversation, "failed", _error_text(e))
		return

	try:
		data = client.execute("messages.send", params)
	except DialogBrainError as e:
		_commit()  # new snapshot: see what the echo did meanwhile
		ambiguous = isinstance(e, DialogBrainTimeout) or e.status in AMBIGUOUS_HTTP_STATUSES
		# ambiguous: stays "unknown" (claimed), only the error text is recorded
		_settle(message_name, msg.conversation, "unknown" if ambiguous else "failed", _error_text(e))
		return
	except Exception as e:  # noqa: BLE001 - the request may have gone out: never resend
		_commit()
		_settle(message_name, msg.conversation, "unknown", _error_text(e))
		frappe.log_error(
			title="DialogBrain send: unexpected error", reference_doctype="DB Message", reference_name=message_name
		)
		return

	_commit()  # new snapshot: _record_sent must see an echo committed during the send
	save_point = "dialogbrain_record_sent"
	frappe.db.savepoint(save_point)
	try:
		_record_sent(message_name, data if isinstance(data, dict) else {}, attachments)
	except Exception:  # noqa: BLE001 - it WAS sent: never leave it failed
		try:
			frappe.db.rollback(save_point=save_point)
		except Exception:  # noqa: BLE001 - e.g. a deadlock already ended the transaction
			frappe.db.rollback()
			return  # the claim (unknown) stands; the echo or reconcile resolves it
		frappe.log_error(
			title="DialogBrain send: recording the result failed",
			reference_doctype="DB Message",
			reference_name=message_name,
		)
		# db_id stays empty: the echo ("sent" is echo-matchable) or reconcile fills it
		_settle(message_name, msg.conversation, "sent")


def _record_sent(name, data, attachments):
	db_id = _s(data.get("db_message_id"))
	message_id = _s(data.get("message_id"))
	# Serialise with the webhook processor working on the same row.
	if not frappe.db.get_value("DB Message", name, "name", for_update=True):
		return  # deleted meanwhile
	doc = frappe.get_doc("DB Message", name)
	conversation = doc.conversation

	if db_id and doc.db_id != db_id:
		if doc.db_id:
			frappe.log_error(
				title="DialogBrain send: db_id mismatch",
				message=f"{name}: row has {doc.db_id}, send returned {db_id}",
				reference_doctype="DB Message",
				reference_name=name,
			)
		else:
			echo = frappe.db.get_value(
				"DB Message", {"db_id": db_id}, ["name", "message_id", "sent_at"], as_dict=True, for_update=True
			)
			if echo:  # an unmatched echo became its own row: fold it into the reply
				frappe.delete_doc("DB Message", echo.name, ignore_permissions=True, force=True)
				publish(conversation, "message_removed", {"message": echo.name})
				doc.sent_at = echo.sent_at or doc.sent_at
				message_id = message_id or echo.message_id
			elif data.get("sent_at"):
				doc.sent_at = to_system_datetime(data["sent_at"])
			doc.db_id = db_id

	if message_id and not doc.message_id:
		doc.message_id = message_id
	if attachments and not doc.attachments:
		doc.set("attachments", attachments)
	if doc.delivery_status != "deleted":
		doc.delivery_status = "sent"
	doc.error = None
	doc.author_type = "manager"
	doc.save(ignore_permissions=True)
	apply_message_counters(conversation, name, silent=True)
	publish(conversation, "message_updated", {"message": name})
