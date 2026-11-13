"""Initial history load (backfill) and the shared history readers used by reconcile.

Shapes: see ``dialogbrain/tests/fixtures/README.md``.

* ``search.threads {since: YYYY-MM-DD, limit: 50, offset}`` pages threads with activity
  since that day (pinned first, then newest activity first).
* ``messages.read_history {thread_id, date_from, limit: 100, offset}`` pages a thread
  newest page first (each page chronological) until ``next_offset`` is null.

read_history messages are not webhook messages (``id`` instead of ``db_id``, full body in
``text``, no ``html``/``from_id``, ``sender{id,name}``); :func:`history_message_to_event`
is the one place that maps them, so :func:`dialogbrain.sync.upsert.upsert_message` keeps a
single (webhook) contract. Authorship then follows the webhook rule (R12): ``sender.id ==
"system"`` and a name other than "You"/"" -> agent, else manager with empty ``sent_by``.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from dialogbrain.client import DialogBrainClient
from dialogbrain.sync.upsert import (
	RETRYABLE_DB_ERRORS,
	_s,
	_same_text,
	apply_message_counters,
	find_conversation,
	rollback_to,
	to_system_datetime,
	upsert_channel,
	upsert_conversation,
	upsert_message,
)

SETTINGS = "DialogBrain Settings"
BACKFILL_JOB = "dialogbrain.sync.backfill.run_backfill"
BACKFILL_JOB_ID = "dialogbrain_backfill"
THREAD_PAGE = 50
HISTORY_PAGE = 100
MAX_PAGES = 10000  # runaway guard against a server that never ends paging
STATUS_LENGTH = 140  # backfill_status is a Data field


# --- small helpers --------------------------------------------------------------


def commit():
	"""Commit per thread in long jobs; skipped under tests (rollback isolation)."""
	if not frappe.flags.in_test:
		frappe.db.commit()


def utc_day(dt) -> str:
	"""System-time naive datetime -> ``YYYY-MM-DD`` of that moment in UTC."""
	dt = get_datetime(dt)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return dt.astimezone(timezone.utc).date().isoformat()


def set_setting(field, value):
	"""Write a server-managed Settings field. ``modified`` is left alone so an admin with
	the Settings form open can still save it (DialogBrainSettings.validate keeps these
	fields from being overwritten by that save)."""
	frappe.db.set_single_value(SETTINGS, field, value, update_modified=False)


def error_text(exc) -> str:
	return (str(exc) or type(exc).__name__).replace("\n", " ")


# --- mapping --------------------------------------------------------------------


def history_message_to_event(msg: dict) -> dict:
	"""``messages.read_history`` message -> webhook-shaped message for ``upsert_message``."""
	sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
	file_ids = [i for i in (msg.get("attachment_file_ids") or []) if i is not None]
	return {
		"db_id": _s(msg.get("id")),
		"message_id": None,  # history carries no channel message ref
		"html": msg.get("text"),  # history text is the full body
		"text": msg.get("text"),
		"sent_at": msg.get("sent_at"),
		"is_outgoing": msg.get("is_outgoing"),
		"from_id": _s(sender.get("id")),
		"from_name": _s(sender.get("name")),
		"sender": {"id": sender.get("id"), "name": sender.get("name")},
		"attachment_file_ids": file_ids,
		"attachments": [{"file_id": str(i)} for i in file_ids],
		"metadata": msg.get("meta"),
		"is_system_event": False,  # not exposed by read_history
		"source": "messages.read_history",
	}


def _thread_dict(history_thread: "dict | None", entry: "dict | None") -> dict:
	"""Thread dict for ``upsert_conversation`` from a search entry and/or read_history.thread."""
	history_thread = history_thread or {}
	entry = entry or {}
	thread = {
		"id": _s(entry.get("id")) or _s(entry.get("thread_id")) or _s(history_thread.get("id")),
		"channel_ref": _s(entry.get("channel_ref")),
		"title": _s(entry.get("title")) or _s(history_thread.get("title")),
	}
	if entry.get("kind") in ("dm", "livechat") and thread["title"]:
		thread["contact_name"] = thread["title"]
	return thread


def _channel_of(history_thread: "dict | None", entry: "dict | None") -> "str | None":
	entry = entry or {}
	history_thread = history_thread or {}
	account = entry.get("account") if isinstance(entry.get("account"), dict) else {}
	account_id = _s(entry.get("channel_account_id")) or _s(account.get("id")) or _s(history_thread.get("account_id"))
	if not account_id:
		return None
	channel_type = _s(entry.get("channel")) or _s(account.get("channel")) or _s(history_thread.get("channel"))
	return upsert_channel(account_id, channel_type, _s(account.get("name")))


def _chronological(msg):
	sent_at = to_system_datetime(msg.get("sent_at")) or datetime.min
	mid = str(msg.get("id"))
	return sent_at, int(mid) if mid.isdigit() else 0


# --- readers --------------------------------------------------------------------


def iter_threads(client, since: str):
	"""Yield ``(entry, total_count)`` for every thread with activity since ``since``."""
	offset = 0
	for _ in range(MAX_PAGES):
		data = client.execute("search.threads", {"since": since, "limit": THREAD_PAGE, "offset": offset}) or {}
		threads = data.get("threads") or []
		for entry in threads:
			yield entry, data.get("total_count")
		if not threads or not data.get("has_more"):
			return
		offset += len(threads)


def read_history(client, thread_id: str, date_from: str):
	"""All messages of a thread from ``date_from`` (UTC day). Returns ``(thread, messages)``
	with messages oldest first."""
	thread, messages, offset = None, [], 0
	for _ in range(MAX_PAGES):
		data = (
			client.execute(
				"messages.read_history",
				{"thread_id": thread_id, "date_from": date_from, "limit": HISTORY_PAGE, "offset": offset},
			)
			or {}
		)
		thread = thread or data.get("thread")
		messages.extend(data.get("messages") or [])
		next_offset = data.get("next_offset")
		if next_offset is None or not data.get("messages"):
			break
		offset = next_offset
	unique = {}
	for msg in messages:  # pages can overlap when new messages arrive while paging
		if msg.get("id") is not None:
			unique[str(msg["id"])] = msg
	ordered = sorted(unique.values(), key=_chronological)
	return thread, ordered


def import_thread(client, thread_id: str, date_from: str, *, entry=None, silent=False) -> "tuple[str | None, int]":
	"""Read a thread from ``date_from`` and upsert it. Returns ``(conversation, new rows)``.

	Messages already mirrored with the same text are skipped (no churn, no re-publish).
	"""
	history_thread, messages = read_history(client, thread_id, date_from)
	channel = _channel_of(history_thread, entry)
	thread = _thread_dict(history_thread, entry)
	if not channel:
		conversation = find_conversation(thread["id"], thread.get("channel_ref"))
		if not conversation:
			raise ValueError(f"DialogBrain thread {thread_id} without a channel account")
	else:
		conversation, _ = upsert_conversation(thread, channel)

	created_count = 0
	for msg in messages:
		event = history_message_to_event(msg)
		current = frappe.db.get_value("DB Message", {"db_id": event["db_id"]}, ["direction", "text"], as_dict=True)
		if current and current.direction and _same_text(current.text, event["html"]):
			continue
		name, created = upsert_message(conversation, event, silent=silent)
		if created:
			apply_message_counters(conversation, name, silent=silent)
			created_count += 1
	return conversation, created_count


# --- backfill -------------------------------------------------------------------


def enqueue_backfill() -> None:
	"""Start (or restart: the manual Re-sync) the backfill. Also clears reconcile's
	failing-thread map, so threads skipped after 5 failures are tried again."""
	set_setting("backfill_status", "queued")
	set_setting("reconcile_failures", None)
	set_setting("reconcile_status", None)
	frappe.enqueue(
		BACKFILL_JOB,
		queue="long",
		timeout=6 * 3600,
		job_id=BACKFILL_JOB_ID,
		deduplicate=True,
		enqueue_after_commit=True,
	)


def run_backfill() -> None:
	"""Silently load ``backfill_days`` of history. Reruns are cheap (upsert by db_id)."""
	days = frappe.db.get_single_value(SETTINGS, "backfill_days") or 30
	since = utc_day(now_datetime() - timedelta(days=int(days)))
	set_setting("backfill_status", "running 0/?")
	commit()
	done, failures, last_error = 0, 0, None
	try:
		client = DialogBrainClient.from_settings()
		for entry, total in iter_threads(client, since):
			thread_id = _s(entry.get("id")) or _s(entry.get("thread_id"))
			save_point = "dialogbrain_backfill"
			for attempt in (1, 2):  # a deadlock with the webhook processor: retry once
				frappe.db.savepoint(save_point)
				try:
					known = find_conversation(
						thread_id, _s(entry.get("channel_ref")), _s(entry.get("channel_account_id"))
					)
					conversation, _ = import_thread(client, thread_id, since, entry=entry, silent=True)
					if not known:
						_seed_unread(conversation, entry)
					break
				except Exception as exc:  # noqa: BLE001 - one bad thread must not stop the load
					rollback_to(save_point)
					if attempt == 1 and isinstance(exc, RETRYABLE_DB_ERRORS):
						continue
					failures += 1
					last_error = error_text(exc)
					frappe.log_error(title=f"DialogBrain backfill: thread {thread_id}")
			done += 1
			set_setting("backfill_status", f"running {done}/{total if total is not None else '?'}")
			commit()
	except Exception as exc:  # noqa: BLE001 - reported in backfill_status
		frappe.db.rollback()
		frappe.log_error(title="DialogBrain backfill failed")
		set_setting("backfill_status", f"error: {error_text(exc)}"[:STATUS_LENGTH])
		commit()
		return
	if failures:
		status = f"error: {failures} of {done} threads failed (last: {last_error})"
	else:
		status = "done"
	set_setting("backfill_status", status[:STATUS_LENGTH])
	commit()


def _seed_unread(conversation, entry):
	"""A new conversation's backfilled inbound messages each counted as unread; use
	DialogBrain's count instead (only for conversations this backfill created)."""
	unread = entry.get("unread_count")
	if conversation and isinstance(unread, int) and unread >= 0:
		frappe.db.set_value("DB Conversation", conversation, "unread_count", unread, update_modified=False)
