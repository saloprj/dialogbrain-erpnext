"""Manager replies: store a pending DB Message, deliver it to DialogBrain in a job.

Never send twice
----------------
* ``client_ref`` (unique) makes :func:`create_reply` idempotent: a double click or a
  re-submitted request returns the existing row and enqueues nothing.
* :func:`deliver` first *claims* the row atomically (``pending`` -> ``unknown``, committed)
  and only then calls ``messages.send``, once. A second job, or a re-run after a killed
  worker, finds ``unknown`` and sends nothing. An ambiguous outcome (read timeout, 502/504
  from the gateway, unexpected exception after the request left, a crash) leaves the row
  ``unknown``: the webhook echo (``upsert_message`` echo matching accepts ``unknown``) or
  reconcile resolves it. While the send is in flight the row is ``unknown`` with an empty
  ``error``.
* Every outcome write after the claim is conditional (row still ``unknown`` and no
  ``db_id``): an echo that completed the row during the send is never overwritten.
* ``failed`` rows without a ``db_id`` (DialogBrain answered with a definite error, or
  nothing was sent) can be retried, via :func:`retry_reply`.

``request_id`` (Plan 3 B4)
--------------------------
When DialogBrain has the ``request_id`` capability, every send carries
``request_id`` = the reply's ``client_ref`` (hashed when it is not a valid id). DialogBrain
caches a *successful* result under it for 24 h, so an ``unknown`` reply can then be retried
too, with the same ``client_ref``. That is allowed only when:

* the first attempt itself carried the ``request_id`` (``raw.send``, written and committed
  before the call) and the capability is still there;
* the first attempt was less than 23 h ago (:data:`REQUEST_ID_RETRY_WINDOW`, inside
  DialogBrain's 24 h cache), checked again when the job runs;
* the last attempt started more than :data:`RETRY_SETTLE_AFTER` ago: DialogBrain has no
  in-flight marker, so a retry next to a still-running send would run concurrently.

The retry re-sends the exact stored params (no new upload, so the same cache key). A
``failed`` reply is retried the same way while its stored attempt is inside the window: the
R11 sweep marks a reply it could not confirm ``failed``, and it may have been sent after all.
Outside the window (or without the capability) the attempt is rebuilt (fresh upload) under
a NEW ``request_id`` (``<ref>:r<n>``), so a surviving cache entry can never stand in for
params that changed.

After every answered send the echo is checked: DialogBrain with A1 returns the
``request_id`` it used. None (e.g. a rolled-back DialogBrain): the attempt is marked
``request_id_ignored`` (never re-sent as deduped) and the capability is cleared until the
next Connect. An ambiguous outcome records its class in ``raw.send.outcome``
(``timeout``/``http_5xx``/``crash``). The echo is checked only when a response body was
read (``client.last_response_parsed``): a connection error says nothing about it. Its
writes are committed at once.

The claim stamps ``raw.claimed_at`` in its own commit, before any upload, on every path.
The R11 sweep skips replies whose last attempt (``last_attempt_at``: the later of
``claimed_at`` and ``raw.send.attempt_at``) is under 10 minutes old, re-checked under the
row lock. A ``failed`` retry is refused within :data:`RETRY_SETTLE_AFTER` of that stamp
unless deliver itself settled it after a definite outcome (``raw.last_outcome`` =
``answered`` / ``not_sent``): such a failure is rebuilt at once under a new round id, since
re-sending params DialogBrain rejected can't help. Only a sweep-failed (or connection-error)
row reuses its stored attempt.
Failures are never cached and the cache can be lost: a retry after a real failure sends
again, as it should, and in the rare case of a lost cache entry it may send twice. Echo
matching stays the second line of defence.
* A row still ``pending`` after 2 minutes lost its job before the claim (queue restart,
  enqueue failure, worker death): the scheduler tick re-enqueues it
  (:func:`requeue_stale_replies`); the claim keeps that safe.

Echo reconciliation (agrees with ``dialogbrain.sync.upsert``)
------------------------------------------------------------
The pending row has ``sent_at`` = now, so an echo that arrives before the send response is
merged into it by ``upsert_message`` (same text, +-120 s). On the response, the row gets
``db_id = db_message_id``; if that ``db_id`` already belongs to another row (an echo that
could not be matched), that row is folded into ours (``sent_by`` kept) and deleted. An echo
arriving after the response finds the row by ``db_id``.
"""

import hashlib
import json
import mimetypes
import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

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
# The send may have happened: the gateway gave up (502/504), or DialogBrain failed after
# the channel accepted the message (500).
AMBIGUOUS_HTTP_STATUSES = (500, 502, 504)
# A pending reply whose deliver job has not claimed it by then is re-enqueued (F3).
STALE_PENDING_AFTER = timedelta(minutes=2)
MAX_ERROR_LENGTH = 1000
# Set by the R11 reconcile sweep on a reply it could not confirm (it may have been sent).
UNCONFIRMED_ERROR = "Not confirmed by DialogBrain — check the chat before resending"
# ``raw.last_outcome`` written by deliver when it settles a reply ``failed`` itself: the job is
# over, and these failures are definite (nothing to dedupe). ``no_response`` (a connection
# error) is failed but may have reached DialogBrain.
DEFINITE_OUTCOMES = ("answered", "not_sent")
# An ``unknown`` reply may be retried with its request_id only this long after its first
# attempt: DialogBrain keeps the result of a send with a request_id for 24 h.
REQUEST_ID_RETRY_WINDOW = timedelta(hours=23)
# ...and only when its last attempt must be over: DialogBrain's send runs up to ~90 s
# (75 s tool timeout + 15 s reconcile).
RETRY_SETTLE_AFTER = timedelta(seconds=120)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def request_id_for(client_ref) -> str:
	"""DialogBrain accepts ``[A-Za-z0-9_.:-]``, at most 128 chars: the ``client_ref`` as is
	when it fits, else a stable hash of it."""
	ref = str(client_ref or "")
	if _REQUEST_ID.match(ref):
		return ref
	return "erpnext-" + hashlib.sha256(ref.encode("utf-8")).hexdigest()[:48]


def _has_request_id() -> bool:
	from dialogbrain.setup.agent_integration import has_capability

	return has_capability("request_id")


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


def _auto_assign(conversation, user) -> bool:
	"""Assign an unassigned conversation to the replying user; True when it did."""
	if frappe.db.get_value("DB Conversation", conversation, "assigned_to"):
		return False
	frappe.db.sql(
		"""UPDATE `tabDB Conversation` SET assigned_to = %s
		WHERE name = %s AND (assigned_to IS NULL OR assigned_to = '')""",
		(user, conversation),
	)
	return frappe.db.get_value("DB Conversation", conversation, "assigned_to") == user


def deliver_job_id(message_name: str) -> str:
	"""One RQ job id per reply: the send, a retry and the stale sweep dedupe on it."""
	return f"dialogbrain_deliver_{message_name}"


def _enqueue(message_name, after_commit=True):
	# at_front: a manager's reply must not wait behind a burst of webhook deliveries,
	# which share the ``short`` queue.
	frappe.enqueue(
		DELIVER_JOB,
		queue="short",
		at_front=True,
		job_id=deliver_job_id(message_name),
		deduplicate=True,
		enqueue_after_commit=after_commit,
		message_name=message_name,
	)


def requeue_stale_replies() -> None:
	"""Re-enqueue replies still ``pending`` after :data:`STALE_PENDING_AFTER` (called by
	the scheduler tick).

	``pending`` means no deliver job has claimed the row yet: its job was lost (queue
	flushed or restarted, enqueue failed after the commit, the worker died before the
	claim). Re-running ``deliver`` is safe: the atomic claim sends a row at most once, and
	the job id dedupes against a job that is still queued.
	"""
	cutoff = now_datetime() - STALE_PENDING_AFTER
	names = frappe.db.sql(
		"""select name from `tabDB Message`
		where delivery_status = 'pending' and direction = 'out' and modified < %s
		order by modified limit 200""",
		(cutoff,),
		pluck=True,
	)
	for name in names:
		_enqueue(name, after_commit=False)


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

	if _auto_assign(conversation, user):
		# the open conversation's header (assignee) refreshes on `conversation`
		publish(conversation, "conversation", {})
	publish(conversation, "message", {"message": doc.name, "direction": "out"})
	_enqueue(doc.name)
	return {"name": doc.name, "delivery_status": doc.delivery_status}


def _raw_dict(raw) -> dict:
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return {}
	return dict(raw) if isinstance(raw, dict) else {}


def _send_state(raw) -> "dict | None":
	"""What the last attempt sent with a ``request_id`` (``raw.send``), or None. An attempt
	whose ``request_id`` DialogBrain did not echo (``request_id_ignored``) counts as none:
	DialogBrain did not dedupe it."""
	state = _raw_dict(raw).get("send")
	if (
		isinstance(state, dict)
		and state.get("request_id")
		and isinstance(state.get("params"), dict)
		and not state.get("request_id_ignored")
	):
		return state
	return None


def last_attempt_at(raw):
	"""When the last attempt started: the later of ``raw.claimed_at`` (stamped by the claim,
	before any upload, on every path) and ``raw.send.attempt_at``. None when never claimed."""
	raw = _raw_dict(raw)
	state = raw.get("send") if isinstance(raw.get("send"), dict) else {}
	stamps = []
	for value in (raw.get("claimed_at"), state.get("attempt_at")):
		try:
			if value:
				stamps.append(get_datetime(value))
		except Exception:  # noqa: BLE001 - unreadable: ignored
			pass
	return max(stamps) if stamps else None


def _reusable(raw) -> "dict | None":
	"""The stored attempt, when re-sending it exactly is deduped by DialogBrain: it carried
	a ``request_id``, the first attempt is < 23 h old and the capability is still set."""
	state = _send_state(raw)
	if state and not _older_than(state.get("first_attempt_at"), REQUEST_ID_RETRY_WINDOW) and _has_request_id():
		return state
	return None


def _older_than(value, age) -> bool:
	try:
		return get_datetime(value) <= now_datetime() - age
	except Exception:  # noqa: BLE001 - unreadable timestamp: treat as too old
		return True


def unknown_retryable(delivery_status, db_id, raw) -> bool:
	"""An ``unknown`` reply whose first attempt carried a ``request_id`` less than 23 h
	ago, while DialogBrain still has the capability (the UI offers Retry on it)."""
	if delivery_status != "unknown" or db_id:
		return False
	return bool(_reusable(raw))


def _require_settled(raw) -> None:
	"""DialogBrain has no in-flight marker: refuse a retry next to an attempt that may still
	be running (started < :data:`RETRY_SETTLE_AFTER` ago)."""
	at = last_attempt_at(raw)
	if at and at > now_datetime() - RETRY_SETTLE_AFTER:
		frappe.throw(_("This message may still be being sent. Try again in a minute."))


def retry_reply(message: str) -> None:
	"""Re-deliver a ``failed`` reply, or an ``unknown`` one that :func:`unknown_retryable`
	allows (DialogBrain dedupes it by ``request_id``). Anything else (pending/sent/other
	unknown), or a row that already has a DialogBrain ``db_id`` (it was delivered), is
	refused."""
	conversation = frappe.db.get_value("DB Message", message, "conversation")
	if not conversation:
		frappe.throw(_("Message not found"), frappe.DoesNotExistError)
	assert_can_access(conversation)
	row = frappe.db.get_value(
		"DB Message", message, ["delivery_status", "db_id", "raw"], as_dict=True, for_update=True
	)
	updates = {"delivery_status": "pending", "error": None}
	raw = _raw_dict(row.raw)
	if row.delivery_status == "failed" and not row.db_id:
		definite = raw.get("last_outcome") in DEFINITE_OUTCOMES
		if not definite:
			# Failed by the sweep (or a connection error): it may have been sent, and with the
			# sweep its deliver may even still run. Wait until the last attempt is over...
			_require_settled(raw)
		# ...then re-send the very same attempt, which DialogBrain dedupes if it did
		# succeed. After a definite failure (DialogBrain answered with an error, or nothing
		# was sent) or outside the window, the attempt is rebuilt under a new request_id.
		if definite or not _reusable(row.raw):
			if raw.pop("send", None) is not None:
				raw["send_round"] = int(raw.get("send_round") or 0) + 1
				updates["raw"] = json.dumps(raw)
	elif unknown_retryable(row.delivery_status, row.db_id, row.raw):
		_require_settled(raw)
	else:
		frappe.throw(_("Only failed messages can be retried"))
	frappe.db.set_value("DB Message", message, updates)
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
		["name", "conversation", "text", "delivery_status", "db_id", "raw", "client_ref"],
		as_dict=True,
		for_update=True,
	)
	if not row or row.delivery_status != "pending" or row.db_id:
		return None
	# Stamp the attempt in the same commit as the claim, before any (slow) upload: the R11
	# sweep and the retry settle check key on it (``last_attempt_at``).
	raw = _raw_dict(row.raw)
	raw["claimed_at"] = str(now_datetime())
	raw.pop("last_outcome", None)
	row.raw = json.dumps(raw)
	frappe.db.set_value("DB Message", name, {"delivery_status": "unknown", "error": None, "raw": row.raw})
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
	return list(_raw_dict(raw).get("file_urls") or [])


def _fresh_request_id(client_ref, raw) -> str:
	"""``client_ref`` for the first attempt. An attempt rebuilt after its stored one could
	no longer be reused (outside the 23 h window, capability gone, request_id ignored) gets
	``<ref>:r<n>``: a surviving cache entry of an earlier attempt must never stand in for
	params that changed (new upload ids)."""
	base = request_id_for(client_ref)
	n = int(_raw_dict(raw).get("send_round") or 0)
	return f"{base[:100]}:r{n}" if n else base


def _set_last_outcome(name, outcome) -> None:
	raw = _raw_dict(frappe.db.get_value("DB Message", name, "raw"))
	raw["last_outcome"] = outcome
	frappe.db.set_value("DB Message", name, "raw", json.dumps(raw), update_modified=False)


def _update_send_state(name, **fields) -> None:
	raw = _raw_dict(frappe.db.get_value("DB Message", name, "raw"))
	if isinstance(raw.get("send"), dict):
		raw["send"].update(fields)
		frappe.db.set_value("DB Message", name, "raw", json.dumps(raw), update_modified=False)


def _outcome_class(exc) -> str:
	if isinstance(exc, DialogBrainTimeout):
		return "timeout"
	if isinstance(exc, DialogBrainError) and exc.status:
		return f"http_{exc.status}"
	return "crash"


def _check_echo(name, client, request_id) -> None:
	"""DialogBrain with Part A1 echoes the request_id it used. No echo (e.g. a rolled-back
	DialogBrain): it does not dedupe, so this attempt is never re-sent as deduped and the
	capability is cleared until the next Connect: later sends behave as in Plan 1."""
	echoed = getattr(client, "last_request_id", None)
	if echoed == request_id:
		return
	_update_send_state(name, request_id_ignored=True)
	from dialogbrain.setup.agent_integration import clear_capability

	clear_capability("request_id")
	_commit()  # durable before _record_sent, whose rollback must not discard it
	frappe.log_error(
		title="DialogBrain ignored request_id: capability cleared",
		message=f"sent {request_id!r}, echoed {echoed!r}",
		reference_doctype="DB Message",
		reference_name=name,
	)


def _store_send_state(name, raw, request_id, params, attachments, previous) -> None:
	"""Record (and commit) what is about to be sent with ``request_id``, before the call:
	an ``unknown`` outcome can then be retried with exactly these params."""
	now = str(now_datetime())
	raw = _raw_dict(raw)
	raw["send"] = {
		"request_id": request_id,
		"params": params,
		"attachments": attachments,
		"first_attempt_at": (previous or {}).get("first_attempt_at") or now,
		"attempt_at": now,
	}
	frappe.db.set_value("DB Message", name, "raw", json.dumps(raw))
	_commit()


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

	previous = _send_state(msg.raw)  # a retry of a stored attempt (unknown, or failed in the window)
	if previous and not _reusable(msg.raw):
		# DialogBrain may no longer dedupe it: sending again could deliver it twice.
		_settle(message_name, msg.conversation, "unknown", _("Not sent again: DialogBrain can no longer recognise a repeat of this message"))
		return

	# Nothing has been sent until messages.send: any failure here is a plain failure.
	try:
		if not conv or not (conv.thread_id or conv.thread_ref):
			raise DialogBrainError("The conversation has no DialogBrain thread id")
		client = DialogBrainClient.from_settings()
		if previous:  # the very same params: same DialogBrain cache key, no new upload
			attachments = list(previous.get("attachments") or [])
			params = previous["params"]
		else:
			attachments = _upload_files(client, _stored_file_urls(msg.raw))
			params = _send_params(conv, msg.text, attachments)
		send_kwargs = {}
		if previous or _has_request_id():
			request_id = previous["request_id"] if previous else _fresh_request_id(msg.client_ref, msg.raw)
			_store_send_state(message_name, msg.raw, request_id, params, attachments, previous)
			send_kwargs["request_id"] = request_id
	except Exception as e:  # noqa: BLE001 - recorded on the row, user can retry
		if _settle(message_name, msg.conversation, "failed", _error_text(e)):
			_set_last_outcome(message_name, "not_sent")
		return

	try:
		data = client.execute("messages.send", params, **send_kwargs)
	except DialogBrainError as e:
		_commit()  # new snapshot: see what the echo did meanwhile
		ambiguous = isinstance(e, DialogBrainTimeout) or e.status in AMBIGUOUS_HTTP_STATUSES
		# ambiguous: stays "unknown" (claimed), only the error text is recorded
		settled = _settle(message_name, msg.conversation, "unknown" if ambiguous else "failed", _error_text(e))
		parsed = getattr(client, "last_response_parsed", False) is True
		if settled and not ambiguous:
			# an HTTP error status or a tool error DialogBrain answered is definite; a
			# connection error (no status, no response read) may have reached it
			_set_last_outcome(message_name, "answered" if (e.status or parsed) else "no_response")
		if settled and send_kwargs:
			if ambiguous:
				_record_outcome(message_name, e)
			elif parsed:  # DialogBrain answered (a tool error): it should have echoed
				_check_echo(message_name, client, send_kwargs["request_id"])
		return
	except Exception as e:  # noqa: BLE001 - the request may have gone out: never resend
		_commit()
		if _settle(message_name, msg.conversation, "unknown", _error_text(e)) and send_kwargs:
			_record_outcome(message_name, e)
		frappe.log_error(
			title="DialogBrain send: unexpected error", reference_doctype="DB Message", reference_name=message_name
		)
		return

	_commit()  # new snapshot: _record_sent must see an echo committed during the send
	if send_kwargs and getattr(client, "last_response_parsed", False) is True:
		_check_echo(message_name, client, send_kwargs["request_id"])
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


def _record_outcome(name, exc) -> None:
	"""Record (and log) the class of an ambiguous attempt. Retry stays allowed for every
	class while the request_id dedupes it; this is for diagnosis only."""
	outcome = _outcome_class(exc)
	_update_send_state(name, outcome=outcome)
	frappe.logger("dialogbrain").info(f"send {name}: ambiguous outcome {outcome}, retryable by request_id")


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
