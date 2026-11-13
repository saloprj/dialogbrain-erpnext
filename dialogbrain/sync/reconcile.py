"""Periodic reconcile (missed webhooks, private sites) and the per-minute scheduler tick.

Change detection (ruling R5)
----------------------------
``search.threads`` has no per-thread activity timestamp and orders ``is_pinned DESC,
last_message_ts DESC`` without exposing ``is_pinned`` (DialogBrain
``threads/list_threads.py``, ``workspace/handlers/thread_search_handler.py``). A pinned,
unchanged thread can therefore precede changed ones, so the pass never stops early: every
thread with activity in the window (``since`` = day of ``last_reconcile_at`` - 1 day, UTC)
is checked with one ``messages.read_history {thread_id, limit: PROBE_LIMIT}`` probe. A
thread needs a sync when

* its ``thread.last_message_ts`` is newer than our newest stored (``db_id``) message, or
* one of the newest ``PROBE_LIMIT`` messages is not mirrored. This catches a missed message
  followed by a stored one (a manager's reply sent from ERPNext in private mode, a webhook
  that arrived after an earlier one was lost), which the timestamp alone cannot see.

Unknown conversations are synced from ``since`` without a probe.

Webhook health (public mode): DialogBrain lists only active subscriptions. When ours is
missing it is re-activated and the pass re-reads every thread of the window from
``since`` (events of the paused period were never delivered, and a later delivered one
would hide them from the probe).

Unconfirmed replies (ruling R11): outgoing rows still ``unknown`` without ``db_id`` and older
than 10 minutes are resolved by reading their thread from the reply's day (echo matching
completes a reply that did arrive); whatever is still unknown after that successful read is
marked ``failed`` (conditional write) so the manager can check the chat and resend.
"""

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from dialogbrain.client import DialogBrainClient
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
from dialogbrain.sync.upsert import _s, find_conversation, to_system_datetime

RECONCILE_JOB = "dialogbrain.sync.reconcile.reconcile"
RECONCILE_JOB_ID = "dialogbrain_reconcile"
PROCESS_JOB = "dialogbrain.sync.processor.process_delivery"
INTERVALS = {"public": 300, "private": 60}
PROBE_LIMIT = 10
MAX_ATTEMPTS = 5
STUCK_AFTER = timedelta(minutes=10)
UNCONFIRMED_AFTER = timedelta(minutes=10)
UNCONFIRMED_ERROR = "Not confirmed by DialogBrain — check the chat before resending"
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
	"""Every minute: retry deliveries and enqueue reconcile when due. Cheap by design: one
	settings read, one indexed log query, at most one redis lookup."""
	settings = _settings()
	if not settings.connected:
		return
	retry_webhook_logs()
	if _backfill_active(settings.backfill_status):
		if is_job_enqueued(BACKFILL_JOB_ID):
			return  # reconcile would re-notify what backfill is loading silently
		set_setting("backfill_status", "error: interrupted (worker stopped), run Re-sync"[:STATUS_LENGTH])
	interval = INTERVALS.get(settings.network_mode or "public", INTERVALS["public"])
	last = settings.last_reconcile_at
	if last and (now_datetime() - get_datetime(last)).total_seconds() < interval:
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
			job_id=f"dialogbrain_delivery_{name}",
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


def reconcile() -> None:
	settings = _settings()
	if not settings.connected or _backfill_active(settings.backfill_status):
		return
	start = now_datetime()
	last = get_datetime(settings.last_reconcile_at) if settings.last_reconcile_at else start
	since = utc_day(last - timedelta(days=1))
	client = DialogBrainClient.from_settings()
	ok = True

	deep = False
	if (settings.network_mode or "public") == "public" and settings.webhook_id:
		try:
			deep = _ensure_webhook_active(client, settings.webhook_id)
		except Exception as exc:  # noqa: BLE001 - recorded; the cursor stays so nothing is skipped
			ok = False
			set_setting("webhook_status", f"check failed {start}: {error_text(exc)}"[:500])
			frappe.log_error(title="DialogBrain reconcile: webhook check")
		commit()

	synced = {}  # conversation -> earliest UTC day read in this pass
	try:
		for entry, _total in iter_threads(client, since):
			ok = _reconcile_thread(client, entry, since, deep, synced) and ok
	except Exception:  # noqa: BLE001 - search failed: retry next pass from the same cursor
		ok = False
		frappe.log_error(title="DialogBrain reconcile: search.threads")

	ok = _resolve_unconfirmed(client, synced) and ok
	if ok:
		set_setting("last_reconcile_at", start)
	commit()


def _ensure_webhook_active(client, webhook_id) -> bool:
	"""Re-activate our subscription if DialogBrain paused it. True when it was paused."""
	active = {str(w.get("id")) for w in (client.list_webhooks() or []) if w.get("active", True)}
	if str(webhook_id) in active:
		return False
	client.activate_webhook(webhook_id)
	set_setting("webhook_status", f"reactivated {now_datetime()} (was paused)")
	return True


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


def _reconcile_thread(client, entry, since, deep, synced) -> bool:
	thread_id = _s(entry.get("id")) or _s(entry.get("thread_id"))
	save_point = "dialogbrain_reconcile"
	frappe.db.savepoint(save_point)
	try:
		conversation = find_conversation(
			thread_id, _s(entry.get("channel_ref")), _s(entry.get("channel_account_id"))
		)
		date_from = since
		if conversation and not deep:
			date_from = _probe(client, thread_id, conversation, since)
			if date_from is None:
				return True
		conversation, _ = import_thread(client, thread_id, date_from, entry=entry, silent=False)
		synced[conversation] = min(date_from, synced.get(conversation, date_from))
		return True
	except Exception:  # noqa: BLE001 - one thread must not stop the pass; cursor stays
		frappe.db.rollback(save_point=save_point)
		frappe.log_error(title=f"DialogBrain reconcile: thread {thread_id}")
		return False
	finally:
		commit()


# --- unconfirmed replies (R11) ----------------------------------------------------


def _resolve_unconfirmed(client, synced) -> bool:
	cutoff = now_datetime() - UNCONFIRMED_AFTER
	rows = frappe.db.sql(
		"""
		select conversation, min(sent_at) from `tabDB Message`
		where direction = 'out' and delivery_status = 'unknown' and ifnull(db_id, '') = ''
		  and sent_at < %s
		group by conversation
		""",
		cutoff,
	)
	ok = True
	for conversation, oldest in rows:
		day = utc_day(oldest)
		if conversation not in synced or synced[conversation] > day:
			conv = frappe.db.get_value(
				"DB Conversation", conversation, ["thread_id", "thread_ref", "channel"], as_dict=True
			)
			key = conv and (conv.thread_id or conv.thread_ref)
			if not key:
				continue
			save_point = "dialogbrain_unconfirmed"
			frappe.db.savepoint(save_point)
			try:
				import_thread(
					client,
					key,
					day,
					entry={"id": conv.thread_id, "channel_ref": conv.thread_ref, "channel_account_id": conv.channel},
					silent=False,
				)
			except Exception:  # noqa: BLE001 - not confirmed either way: leave it unknown
				frappe.db.rollback(save_point=save_point)
				frappe.log_error(title=f"DialogBrain reconcile: unconfirmed replies of {conversation}")
				ok = False
				commit()
				continue
			synced[conversation] = day
		_fail_unconfirmed(conversation, cutoff)
		commit()
	return ok


def _fail_unconfirmed(conversation, cutoff):
	names = frappe.get_all(
		"DB Message",
		filters={
			"conversation": conversation,
			"direction": "out",
			"delivery_status": "unknown",
			"db_id": ["is", "not set"],
			"sent_at": ["<", cutoff],
		},
		pluck="name",
	)
	for name in names:
		row = frappe.db.get_value("DB Message", name, ["delivery_status", "db_id"], as_dict=True, for_update=True)
		if not row or row.delivery_status != "unknown" or row.db_id:
			continue  # the history read (echo matching) completed it meanwhile
		frappe.db.set_value("DB Message", name, {"delivery_status": "failed", "error": UNCONFIRMED_ERROR})
		publish(conversation, "message_updated", {"message": name})
