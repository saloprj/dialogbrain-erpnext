"""Periodic reconcile (missed webhooks, private sites) and the per-minute scheduler tick.

Change detection (ruling R5)
----------------------------
``search.threads`` has no per-thread activity timestamp and orders ``is_pinned DESC,
last_message_ts DESC`` without exposing ``is_pinned`` (DialogBrain
``threads/list_threads.py``, ``workspace/handlers/thread_search_handler.py``). A pinned,
unchanged thread can therefore precede changed ones, so the pass never stops early: every
thread with activity in the window (``since`` = UTC day of ``last_reconcile_at`` - 1 day)
is checked with one ``messages.read_history {thread_id, limit: PROBE_LIMIT}`` probe. A
thread needs a sync when

* its ``thread.last_message_ts`` is newer than our newest stored (``db_id``) message, or
* one of the newest ``PROBE_LIMIT`` messages is not mirrored. This catches a missed message
  followed by a stored one (a manager's reply sent from ERPNext in private mode, a webhook
  that arrived after an earlier one was lost), which the timestamp alone cannot see.

Unknown conversations are synced from ``since`` without a probe. ``since`` is never older
than ``backfill_days`` (a long outage does not page the whole history).

Cursor and failing threads (ruling R13)
---------------------------------------
``last_reconcile_at`` advances when the global steps succeed (webhook check, every
``search.threads`` page). A thread that fails (probe, read, upsert) is rolled back and
recorded in ``reconcile_failures`` (thread key -> ``{count, date_from, error, last_at,
entry}``); each pass first retries recorded threads from their ``date_from``, whether or not
they are still in the window. Success clears the entry; after 5 consecutive failures the
thread is skipped (kept and shown in ``reconcile_status``) until a manual Re-sync
(:func:`dialogbrain.sync.backfill.enqueue_backfill`) clears the map.

Webhook health (public mode): DialogBrain lists only active subscriptions. When ours is
missing it is re-activated and the pass re-reads every thread of the window from
``since`` (events of the paused period were never delivered, and a later delivered one
would hide them from the probe). A subscription deleted at DialogBrain (activate answers 404)
is forgotten (id/secret cleared, status "missing … run Reconnect") without failing the
pass, so the cursor keeps moving; reconcile then polls until the admin reconnects.

Unconfirmed replies (ruling R11): outgoing rows still ``unknown`` without ``db_id``, older
than 10 minutes and whose last send attempt (``raw.send.attempt_at``, Plan 3 B4) is not
under 10 minutes old, are resolved by reading their thread from the reply's day (echo matching
completes a reply that did arrive); whatever is still unknown after that successful read is
marked ``failed`` (conditional write) so the manager can check the chat and resend.
"""

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued
from frappe.utils.password import remove_encrypted_password

from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.sync.backfill import (
	BACKFILL_JOB_ID,
	SETTINGS,
	STATUS_LENGTH,
	commit,
	error_text,
	import_thread,
	iter_threads,
	set_setting,
	utc_day,
)
from dialogbrain.sync.notify import publish
from dialogbrain.sync.processor import MAX_ATTEMPTS, delivery_job_id
from dialogbrain.sync.sender import last_attempt_at, requeue_stale_replies
from dialogbrain.sync.upsert import _s, find_conversation, rollback_to, to_system_datetime

RECONCILE_JOB = "dialogbrain.sync.reconcile.reconcile"
RECONCILE_JOB_ID = "dialogbrain_reconcile"
PROCESS_JOB = "dialogbrain.sync.processor.process_delivery"
INTERVALS = {"public": 300, "private": 60}
# The cursor is a pass's start, a moment after the tick that enqueued it; without slack the
# next 60 s tick sees 59.x s and every other minute is skipped.
TICK_SLACK = 5
PROBE_LIMIT = 10
STUCK_AFTER = timedelta(minutes=10)
UNCONFIRMED_AFTER = timedelta(minutes=10)
MAX_THREAD_FAILURES = 5
STATUS_TEXT_LENGTH = 1000
UNCONFIRMED_ERROR = "Not confirmed by DialogBrain — check the chat before resending"
WEBHOOK_MISSING_STATUS = "missing: the DialogBrain webhook was deleted — run Reconnect"
SETTINGS_FIELDS = (
	"connected",
	"network_mode",
	"last_reconcile_at",
	"backfill_status",
	"webhook_id",
)


def _settings():
	return frappe.db.get_value(SETTINGS, SETTINGS, list(SETTINGS_FIELDS), as_dict=True) or frappe._dict()


def _backfill_active(status) -> bool:
	return bool(status) and (status == "queued" or status.startswith("running"))


# --- tick -----------------------------------------------------------------------


def tick() -> None:
	"""Every minute: retry deliveries, re-enqueue stale pending replies, enqueue reconcile
	when due. Cheap by design: one settings read, two indexed queries, a redis lookup per
	re-enqueued job."""
	settings = _settings()
	if not settings.connected:
		return
	retry_webhook_logs()
	requeue_stale_replies()
	if _backfill_active(settings.backfill_status):
		if is_job_enqueued(BACKFILL_JOB_ID):
			return  # reconcile would re-notify what backfill is loading silently
		set_setting("backfill_status", "error: interrupted (worker stopped), run Re-sync"[:STATUS_LENGTH])
	interval = INTERVALS.get(settings.network_mode or "public", INTERVALS["public"])
	last = settings.last_reconcile_at
	if last and (now_datetime() - get_datetime(last)).total_seconds() < interval - TICK_SLACK:
		return
	frappe.enqueue(RECONCILE_JOB, queue="long", timeout=3600, job_id=RECONCILE_JOB_ID, deduplicate=True)


def retry_webhook_logs() -> None:
	"""Re-enqueue failed deliveries and ones stuck ``queued`` > 10 min (``attempts < 5``)."""
	stuck_before = now_datetime() - STUCK_AFTER
	names = frappe.db.sql(
		"""
		select name from `tabDB Webhook Log`
		where ifnull(attempts, 0) < %(max)s
		  and (status = 'failed' or (status = 'queued' and received_at < %(stuck)s))
		order by received_at
		limit 500
		""",
		{"max": MAX_ATTEMPTS, "stuck": stuck_before},
		pluck=True,
	)
	for name in names:
		frappe.enqueue(
			PROCESS_JOB,
			queue="short",
			job_id=delivery_job_id(name),
			deduplicate=True,
			log_name=name,
		)


# --- reconcile ------------------------------------------------------------------


def sync_thread(thread_id: str, date_from: str, *, entry: "dict | None" = None, client=None) -> int:
	"""Read a thread from ``date_from`` (UTC day) and upsert it, not silently.

	Returns the number of new rows. ``entry`` (a ``search.threads`` row or ``{channel_ref,
	channel_account_id}``) keeps the conversation keyed by its channel ref.
	"""
	client = client or DialogBrainClient.from_settings()
	_, created = import_thread(client, thread_id, date_from, entry=entry, silent=False)
	return created


class Pass:
	"""State of one reconcile pass."""

	def __init__(self, client, since, deep, failures):
		self.client = client
		self.since = since
		self.deep = deep
		self.failures = failures  # thread key -> {count, date_from, error, last_at, entry}
		self.synced = {}  # conversation -> earliest UTC day read in this pass
		self.handled = set()  # thread keys already dealt with in this pass

	def skipped(self, key) -> bool:
		return (self.failures.get(key) or {}).get("count", 0) >= MAX_THREAD_FAILURES

	def import_(self, key, date_from, entry):
		"""import_thread inside a savepoint; failures are recorded, never raised."""
		save_point = "dialogbrain_reconcile"
		frappe.db.savepoint(save_point)
		try:
			conversation, _ = import_thread(self.client, key, date_from, entry=entry, silent=False)
		except Exception as exc:  # noqa: BLE001 - recorded per thread (R13)
			rollback_to(save_point)
			frappe.log_error(title=f"DialogBrain reconcile: thread {key}")
			self.fail(key, date_from, entry, exc)
			return None
		finally:
			commit()
		self.failures.pop(key, None)
		self.synced[conversation] = min(date_from, self.synced.get(conversation, date_from))
		return conversation

	def fail(self, key, date_from, entry, exc):
		record = self.failures.get(key) or {}
		previous = record.get("date_from")
		self.failures[key] = {
			"count": int(record.get("count") or 0) + 1,
			"date_from": min(previous, date_from) if previous else date_from,
			"error": error_text(exc)[:300],
			"last_at": str(now_datetime()),
			"entry": _identity(entry),
		}


def _identity(entry):
	"""The fields of a thread entry needed to re-import it outside the search window."""
	entry = entry or {}
	keep = ("id", "thread_id", "channel_ref", "channel_account_id", "channel", "title", "kind")
	return {k: entry.get(k) for k in keep if entry.get(k) is not None}


def _load_failures(value) -> dict:
	try:
		failures = frappe.parse_json(value) if value else {}
	except Exception:  # noqa: BLE001 - a corrupt value must not stop reconcile
		failures = {}
	return failures if isinstance(failures, dict) else {}


def _status_text(failures) -> str:
	if not failures:
		return "OK"
	active = {k: r for k, r in failures.items() if (r.get("count") or 0) < MAX_THREAD_FAILURES}
	skipped = {k: r for k, r in failures.items() if k not in active}
	parts = []
	if active:
		parts.append(
			f"{len(active)} thread(s) failing, retried every pass: "
			+ "; ".join(f"{k} ({r.get('count')}x: {(r.get('error') or '')[:80]})" for k, r in active.items())
		)
	if skipped:
		parts.append(
			f"{len(skipped)} thread(s) skipped after {MAX_THREAD_FAILURES} failures, run Re-sync to retry: "
			+ "; ".join(f"{k} ({(r.get('error') or '')[:80]})" for k, r in skipped.items())
		)
	return " | ".join(parts)[:STATUS_TEXT_LENGTH]


def reconcile() -> None:
	"""One pass. The cursor (``last_reconcile_at``) advances when the global steps succeed
	(webhook check, every ``search.threads`` page); a failing thread is recorded in
	``reconcile_failures`` and retried from its own date on later passes (R13)."""
	settings = frappe.db.get_value(
		SETTINGS, SETTINGS, [*SETTINGS_FIELDS, "backfill_days", "reconcile_failures"], as_dict=True
	) or frappe._dict()
	if not settings.connected or _backfill_active(settings.backfill_status):
		return
	start = now_datetime()
	last = get_datetime(settings.last_reconcile_at) if settings.last_reconcile_at else start
	floor = utc_day(start - timedelta(days=int(settings.backfill_days or 30)))
	since = max(utc_day(last - timedelta(days=1)), floor)
	client = DialogBrainClient.from_settings()
	global_ok = True

	deep = False
	if (settings.network_mode or "public") == "public" and settings.webhook_id:
		try:
			deep = _ensure_webhook_active(client, settings.webhook_id)
		except Exception as exc:  # noqa: BLE001 - recorded; the cursor stays so nothing is skipped
			global_ok = False
			set_setting("webhook_status", f"check failed {start}: {error_text(exc)}"[:500])
			frappe.log_error(title="DialogBrain reconcile: webhook check")
		commit()

	run = Pass(client, since, deep, _load_failures(settings.reconcile_failures))

	# 1. threads that failed before: retried from their own date, inside the window or not
	for key, record in list(run.failures.items()):
		run.handled.add(key)
		if not run.skipped(key):
			run.import_(key, record.get("date_from") or since, record.get("entry"))

	# 2. every thread with activity in the window
	try:
		for entry, _total in iter_threads(client, since):
			key = _s(entry.get("id")) or _s(entry.get("thread_id"))
			if not key or key in run.handled:
				continue
			run.handled.add(key)
			_reconcile_thread(run, key, entry)
	except Exception:  # noqa: BLE001 - search failed: retry next pass from the same cursor
		global_ok = False
		frappe.log_error(title="DialogBrain reconcile: search.threads")

	# 3. unconfirmed replies (R11)
	_resolve_unconfirmed(run)

	set_setting("reconcile_failures", frappe.as_json(run.failures) if run.failures else None)
	set_setting("reconcile_status", _status_text(run.failures))
	if global_ok:
		set_setting("last_reconcile_at", start)
	commit()


def _ensure_webhook_active(client, webhook_id) -> bool:
	"""Re-activate our subscription if DialogBrain paused it. True when it was paused (or
	is gone): events may have been lost, so the pass re-reads the whole window.

	A 404 on activate means the subscription was deleted on the DialogBrain side: retrying
	every pass would keep the cursor frozen forever. Its id and secret are cleared and
	Settings shows :data:`WEBHOOK_MISSING_STATUS` (Reconnect creates a new one); until then
	reconcile polls like private mode, every public interval.
	"""
	active = {str(w.get("id")) for w in (client.list_webhooks() or []) if w.get("active", True)}
	if str(webhook_id) in active:
		return False
	try:
		client.activate_webhook(webhook_id)
	except DialogBrainError as exc:
		if exc.status != 404:
			raise
		_forget_webhook()
		return True
	set_setting("webhook_status", f"reactivated {now_datetime()} (was paused)")
	return True


def _forget_webhook():
	set_setting("webhook_id", None)
	set_setting("webhook_secret", None)
	remove_encrypted_password(SETTINGS, SETTINGS, "webhook_secret")
	set_setting("webhook_status", WEBHOOK_MISSING_STATUS)
	frappe.log_error(
		title="DialogBrain webhook deleted",
		message="The DialogBrain webhook subscription no longer exists (404). Run Reconnect in DialogBrain Settings.",
	)


def _newest_stored(conversation):
	row = frappe.db.sql(
		"""select max(sent_at) from `tabDB Message`
		where conversation = %s and ifnull(db_id, '') != ''""",
		conversation,
	)
	return get_datetime(row[0][0]) if row and row[0][0] else None


def _probe(client, thread_id, conversation, since) -> "str | None":
	"""UTC day to sync from, or None when the thread is unchanged."""
	data = client.execute("messages.read_history", {"thread_id": thread_id, "limit": PROBE_LIMIT}) or {}
	newest = _newest_stored(conversation)
	days = []
	last_ts = to_system_datetime((data.get("thread") or {}).get("last_message_ts"))
	if last_ts and (not newest or last_ts > newest):
		days.append(utc_day(newest) if newest else since)
	messages = [m for m in (data.get("messages") or []) if m.get("id") is not None]
	ids = [str(m["id"]) for m in messages]
	known = set(frappe.get_all("DB Message", filters={"db_id": ["in", ids]}, pluck="db_id")) if ids else set()
	for msg in messages:
		if str(msg["id"]) not in known:
			sent_at = to_system_datetime(msg.get("sent_at"))
			days.append(utc_day(sent_at) if sent_at else since)
	return min(days) if days else None


def _reconcile_thread(run, key, entry):
	conversation = find_conversation(key, _s(entry.get("channel_ref")), _s(entry.get("channel_account_id")))
	date_from = run.since
	if conversation and not run.deep:
		try:
			date_from = _probe(run.client, key, conversation, run.since)
		except Exception as exc:  # noqa: BLE001 - recorded per thread (R13)
			frappe.log_error(title=f"DialogBrain reconcile: probe {key}")
			run.fail(key, run.since, entry, exc)
			return
		if date_from is None:
			return
	run.import_(key, date_from, entry)


# --- unconfirmed replies (R11) ----------------------------------------------------


def _attempt_recent(raw, cutoff) -> bool:
	"""B4: a retried reply keeps its old ``sent_at``; its last attempt (``raw.send``) may
	still be in flight. Unreadable timestamps count as old."""
	at = last_attempt_at(raw)
	if not at:
		return False
	try:
		return get_datetime(at) >= cutoff
	except Exception:  # noqa: BLE001
		return False


def _unconfirmed(cutoff, conversation=None):
	"""``unknown`` replies without ``db_id``, sent before ``cutoff`` and whose last
	attempt is not recent: ``[(name, conversation, sent_at)]``."""
	cond = " and conversation = %(conversation)s" if conversation else ""
	rows = frappe.db.sql(
		f"""
		select name, conversation, sent_at, raw from `tabDB Message`
		where direction = 'out' and delivery_status = 'unknown' and ifnull(db_id, '') = ''
		  and sent_at < %(cutoff)s{cond}
		""",
		{"cutoff": cutoff, "conversation": conversation},
	)
	return [(n, c, t) for n, c, t, raw in rows if not _attempt_recent(raw, cutoff)]


def _resolve_unconfirmed(run):
	cutoff = now_datetime() - UNCONFIRMED_AFTER
	oldest_by_conv = {}
	for _name, conversation, sent_at in _unconfirmed(cutoff):
		if conversation not in oldest_by_conv or sent_at < oldest_by_conv[conversation]:
			oldest_by_conv[conversation] = sent_at
	for conversation, oldest in oldest_by_conv.items():
		day = utc_day(oldest)
		if conversation not in run.synced or run.synced[conversation] > day:
			conv = frappe.db.get_value(
				"DB Conversation", conversation, ["thread_id", "thread_ref", "channel"], as_dict=True
			)
			key = conv and (conv.thread_id or conv.thread_ref)
			if not key or run.skipped(key):
				continue
			entry = {"id": conv.thread_id, "channel_ref": conv.thread_ref, "channel_account_id": conv.channel}
			if not run.import_(key, day, entry):
				continue  # not confirmed either way: leave it unknown
		_fail_unconfirmed(conversation, cutoff)
		commit()


def _fail_unconfirmed(conversation, cutoff):
	names = [name for name, _conv, _sent in _unconfirmed(cutoff, conversation)]
	for name in names:
		row = frappe.db.get_value("DB Message", name, ["delivery_status", "db_id"], as_dict=True, for_update=True)
		if not row or row.delivery_status != "unknown" or row.db_id:
			continue  # the history read (echo matching) completed it meanwhile
		frappe.db.set_value("DB Message", name, {"delivery_status": "failed", "error": UNCONFIRMED_ERROR})
		publish(conversation, "message_updated", {"message": name})
